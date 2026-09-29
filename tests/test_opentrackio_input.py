#!/usr/bin/env python3
"""Tests for OpenTrackIO input: header, reassembly, conversion, detection, listener."""
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.protocol import (FreeDParser, UdpListener, detect_protocol,
                          PROTO_FREED, PROTO_OTI)
from src.opentrackio import (OpenTrackIOParser, OpenTrackIOSender, fletcher16,
                             parse_oti_header, oti_to_freed_packet, oti_timecode,
                             oti_device_label, oti_camera_transform)
from tests.test_freed import _import_freed_reader

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def load_sample() -> dict:
    """A real sample captured from a Sony Ocellus ASR-CT1."""
    with open(os.path.join(_DATA, 'ocellus_sample.json'), encoding='utf-8') as fh:
        return json.load(fh)


def build_segments(payload: bytes, seq: int, seg_size: int = 1200, encoding: int = 0x01) -> list:
    """Split a payload into OpenTrackIO datagrams the way the Ocellus does."""
    out = []
    for off in range(0, max(len(payload), 1), seg_size):
        chunk = payload[off:off + seg_size]
        last  = off + seg_size >= len(payload)
        hdr = bytearray(14)
        hdr[0:4] = b'OTrk'
        hdr[5] = encoding
        struct.pack_into('>H', hdr, 6, seq)
        struct.pack_into('>I', hdr, 8, off)
        struct.pack_into('>H', hdr, 12, (0x8000 if last else 0) | len(chunk))
        out.append(bytes(hdr) + struct.pack('>H', fletcher16(bytes(hdr) + chunk)) + chunk)
    return out


def sample_bytes(sample: dict = None) -> bytes:
    return json.dumps(sample or load_sample(), separators=(',', ':')).encode('utf-8')


# ---------------------------------------------------------------------------
# Header + detection
# ---------------------------------------------------------------------------

class TestOTIHeader(unittest.TestCase):

    def test_parse_segment_header(self):
        segs = build_segments(sample_bytes(), seq=4291)
        self.assertEqual(len(segs), 2)
        h0, h1 = parse_oti_header(segs[0]), parse_oti_header(segs[1])
        self.assertEqual((h0['seq'], h0['offset'], h0['last'], h0['length']), (4291, 0, False, 1200))
        self.assertEqual((h1['offset'], h1['last']), (1200, True))
        self.assertTrue(h0['checksum_ok'] and h1['checksum_ok'])

    def test_sender_packets_parse_back(self):
        """Packets built by our own OpenTrackIOSender must be readable by the parser."""
        s = OpenTrackIOSender()
        try:
            payload = b'{"a":1}'
            h = parse_oti_header(s._build_packet(payload, 7))
            self.assertTrue(h['checksum_ok'])
            self.assertTrue(h['last'])
            self.assertEqual(h['payload'], payload)
        finally:
            s.close()

    def test_corrupt_checksum_detected(self):
        seg = bytearray(build_segments(b'{"x":1}', seq=1)[0])
        seg[-1] ^= 0xFF
        self.assertFalse(parse_oti_header(bytes(seg))['checksum_ok'])

    def test_not_oti(self):
        self.assertIsNone(parse_oti_header(b'\xD1' + bytes(28)))
        self.assertIsNone(parse_oti_header(b'OTrk'))

    def test_detect_protocol(self):
        self.assertEqual(detect_protocol(build_segments(b'{}', 1)[0]), PROTO_OTI)
        self.assertEqual(detect_protocol(b'\xD1' + bytes(28)), PROTO_FREED)
        self.assertIsNone(detect_protocol(b'\xD1' + bytes(10)))     # too short for FreeD
        self.assertIsNone(detect_protocol(b'hello world, not tracking data'))


# ---------------------------------------------------------------------------
# Reassembly
# ---------------------------------------------------------------------------

class TestOTIReassembly(unittest.TestCase):

    def test_two_segments_in_order(self):
        p = OpenTrackIOParser()
        s0, s1 = build_segments(sample_bytes(), seq=10)
        self.assertIsNone(p.feed(s0, ('a', 1), 0.0))
        smp = p.feed(s1, ('a', 1), 0.001)
        self.assertIsNotNone(smp)
        self.assertEqual(smp['_meta']['segments'], 2)
        self.assertEqual(oti_camera_transform(smp)['id'], 'camera')
        self.assertEqual(p.sample_count, 1)

    def test_out_of_order_segments(self):
        p = OpenTrackIOParser()
        s0, s1 = build_segments(sample_bytes(), seq=10)
        self.assertIsNone(p.feed(s1, ('a', 1), 0.0))
        self.assertIsNotNone(p.feed(s0, ('a', 1), 0.001))

    def test_missing_middle_segment_expires(self):
        p = OpenTrackIOParser()
        segs = build_segments(sample_bytes(), seq=3, seg_size=500)
        self.assertEqual(len(segs), 3)
        self.assertIsNone(p.feed(segs[0], ('a', 1), 0.0))
        self.assertIsNone(p.feed(segs[2], ('a', 1), 0.01))
        # Next message arrives after the timeout — the stale partial is dropped
        self.assertIsNotNone(p.feed(build_segments(b'{"ok":1}', seq=4)[0], ('a', 1), 1.0))
        self.assertEqual(p.incomplete_count, 1)

    def test_unsegmented(self):
        p = OpenTrackIOParser()
        smp = p.feed(build_segments(b'{"sourceNumber":2}', seq=1)[0])
        self.assertEqual(smp['sourceNumber'], 2)
        self.assertEqual(smp['_meta']['segments'], 1)

    def test_interleaved_senders_do_not_mix(self):
        p = OpenTrackIOParser()
        a0, a1 = build_segments(sample_bytes(), seq=5)
        b0, b1 = build_segments(sample_bytes(), seq=5)
        self.assertIsNone(p.feed(a0, ('A', 1), 0.0))
        self.assertIsNone(p.feed(b0, ('B', 1), 0.0))
        self.assertIsNotNone(p.feed(a1, ('A', 1), 0.0))
        self.assertIsNotNone(p.feed(b1, ('B', 1), 0.0))

    def test_seq_gap_counted(self):
        p = OpenTrackIOParser()
        for seq in (1, 2, 5):
            p.feed(build_segments(b'{}', seq)[0], ('a', 1))
        self.assertEqual(p.seq_gaps, 2)

    def test_seq_wraparound_is_not_a_gap(self):
        p = OpenTrackIOParser()
        for seq in (0xFFFE, 0xFFFF, 0, 1):
            p.feed(build_segments(b'{}', seq)[0], ('a', 1))
        self.assertEqual(p.seq_gaps, 0)

    def test_bad_checksum_counted_and_dropped(self):
        p = OpenTrackIOParser()
        seg = bytearray(build_segments(b'{"x":1}', 1)[0])
        seg[20] ^= 0x01
        self.assertIsNone(p.feed(bytes(seg)))
        self.assertEqual(p.checksum_errors, 1)

    def test_bad_json_counted(self):
        p = OpenTrackIOParser()
        self.assertIsNone(p.feed(build_segments(b'{not json', 1)[0]))
        self.assertEqual(p.decode_errors, 1)


# ---------------------------------------------------------------------------
# Sample helpers + conversion to FreeD
# ---------------------------------------------------------------------------

class TestOTIToFreeD(unittest.TestCase):

    def setUp(self):
        self.sample = load_sample()
        self.parsed = FreeDParser().parse(oti_to_freed_packet(self.sample))

    def test_packet_is_valid_d1(self):
        pkt = oti_to_freed_packet(self.sample)
        self.assertEqual(len(pkt), 29)
        self.assertEqual(pkt[0], 0xD1)
        self.assertTrue(self.parsed['checksum_valid'])
        self.assertIsNone(self.parsed['ext_tc'])        # no timecode on converted packets

    def test_values_round_trip(self):
        t = oti_camera_transform(self.sample)
        d = self.parsed
        self.assertAlmostEqual(d['pan']  / 32768, t['rotation']['pan'],  places=4)
        self.assertAlmostEqual(d['tilt'] / 32768, t['rotation']['tilt'], places=4)
        self.assertAlmostEqual(d['roll'] / 32768, t['rotation']['roll'], places=4)
        for axis in 'xyz':
            self.assertAlmostEqual(d['position'][axis] / 64000, t['translation'][axis], places=4)
        # Zoom / focus carry the raw lens encoder counts, like the Ocellus's own FreeD output
        self.assertEqual(d['zoom'],  self.sample['lens']['rawEncoders']['zoom'])
        self.assertEqual(d['focus'], self.sample['lens']['rawEncoders']['focus'])
        self.assertEqual(d['camera_id'], self.sample['sourceNumber'])

    def test_standard_checksum(self):
        pkt = oti_to_freed_packet(self.sample)
        self.assertEqual(pkt[28], (0x40 - sum(pkt[:28])) & 0xFF)
        self.assertEqual(self.parsed['checksum_scheme'], 'standard')

    def test_lens_falls_back_to_normalised_encoders(self):
        del self.sample['lens']['rawEncoders']
        self.sample['lens']['encoders'] = {'zoom': 0.5, 'focus': 1.0}
        d = FreeDParser().parse(oti_to_freed_packet(self.sample))
        self.assertEqual(d['zoom'], round(0.5 * 65535))
        self.assertEqual(d['focus'], 65535)

    def test_genlock_phase_cycles_when_locked(self):
        phases = set()
        for n in range(16):
            self.sample['timing']['sequenceNumber'] = n
            phases.add(oti_to_freed_packet(self.sample)[26] >> 4)
        self.assertEqual(len(phases), 16)

    def test_genlock_phase_constant_when_unlocked(self):
        self.sample['timing']['synchronization']['locked'] = False
        phases = set()
        for n in range(16):
            self.sample['timing']['sequenceNumber'] = n
            phases.add(oti_to_freed_packet(self.sample)[26] >> 4)
        self.assertEqual(phases, {0})

    def test_pan_beyond_24bit_range_wraps(self):
        self.sample['transforms'][0]['rotation']['pan'] = 370.0
        d = FreeDParser().parse(oti_to_freed_packet(self.sample))
        self.assertAlmostEqual(d['pan'] / 32768, 10.0, places=3)

    def test_empty_sample_does_not_crash(self):
        self.assertTrue(FreeDParser().parse(oti_to_freed_packet({}))['checksum_valid'])

    def test_timecode_and_label(self):
        self.assertEqual(oti_timecode(self.sample), (12, 52, 46, 12))
        self.assertIsNone(oti_timecode({}))
        self.assertIn('ASR-CT1', oti_device_label(self.sample))
        self.assertEqual(oti_device_label({}), 'OpenTrackIO')

    def test_camera_transform_selection(self):
        smp = {'transforms': [{'id': 'stage'}, {'id': 'Camera_A'}, {'id': 'other'}]}
        self.assertEqual(oti_camera_transform(smp)['id'], 'Camera_A')
        smp = {'transforms': [{'id': 'a'}, {'id': 'b'}]}
        self.assertEqual(oti_camera_transform(smp)['id'], 'b')
        self.assertEqual(oti_camera_transform({}), {})


# ---------------------------------------------------------------------------
# UdpListener over loopback
# ---------------------------------------------------------------------------

class TestUdpListener(unittest.TestCase):

    @staticmethod
    def _free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _run(self, accept, datagrams, ip_filter=None):
        got = {PROTO_FREED: [], PROTO_OTI: []}
        port = self._free_port()
        lst = UdpListener(port, accept, {
            PROTO_FREED: lambda d, a, t: got[PROTO_FREED].append(d),
            PROTO_OTI:   lambda d, a, t: got[PROTO_OTI].append(d),
        }, host='127.0.0.1')
        if ip_filter:
            lst.ip_filter = ip_filter
        lst.start()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for d in datagrams:
                tx.sendto(d, ('127.0.0.1', port))
            deadline = time.time() + 1.0
            while time.time() < deadline and \
                    sum(s['datagrams'] for s in lst.senders()) < len(datagrams):
                time.sleep(0.01)
        finally:
            tx.close()
            lst.stop()
        return lst, got

    def test_routes_by_protocol_and_reports_mismatch(self):
        oti = build_segments(sample_bytes(), seq=1)
        lst, got = self._run({PROTO_FREED}, [b'\xD1' + bytes(28)] + oti)
        self.assertEqual(len(got[PROTO_FREED]), 1)
        self.assertEqual(got[PROTO_OTI], [])
        self.assertIsNotNone(lst.mismatch)
        self.assertEqual(lst.mismatch[0], PROTO_OTI)

    def test_accepts_both_on_one_port(self):
        oti = build_segments(sample_bytes(), seq=1)
        _, got = self._run({PROTO_FREED, PROTO_OTI}, [b'\xD1' + bytes(28)] + oti)
        self.assertEqual(len(got[PROTO_FREED]), 1)
        self.assertEqual(len(got[PROTO_OTI]), 2)
        self.assertEqual(len(got[PROTO_OTI][0]), 1216)   # large datagram arrives intact

    def test_ip_filter_blocks_other_senders(self):
        lst, got = self._run({PROTO_FREED}, [b'\xD1' + bytes(28)],
                             ip_filter={PROTO_FREED: '10.0.0.99'})
        self.assertEqual(got[PROTO_FREED], [])
        self.assertEqual(len(lst.senders()), 1)          # still shown in the sender table

    def test_sender_rate_counts_messages_not_segments(self):
        datagrams = []
        for seq in range(5):
            datagrams += build_segments(sample_bytes(), seq=seq)
        lst, _ = self._run({PROTO_OTI}, datagrams)
        s = lst.senders()[0]
        self.assertEqual(s['datagrams'], 10)
        self.assertEqual(s['messages'], 5)


# ---------------------------------------------------------------------------
# Forwarder: OTI-converted packets go out without TC injection; new config
# ---------------------------------------------------------------------------

class TestForwarderOTI(unittest.TestCase):

    def setUp(self):
        self.fr  = _import_freed_reader()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inject_tc_false_sends_plain_29_bytes(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(('127.0.0.1', 0))
        rx.settimeout(2.0)
        fwd = self.fr.FreeDForwarder(config_path=os.path.join(self.tmp, 'c.json'))
        try:
            fwd.tc_inject = True
            fwd.tc_source = 'system'
            fwd.destinations = [{'ip': '127.0.0.1', 'port': rx.getsockname()[1], 'enabled': True}]
            pkt = oti_to_freed_packet(load_sample())
            fwd.forward(pkt, None, inject_tc=False)
            self.assertEqual(rx.recvfrom(100)[0], pkt)
            fwd.forward(pkt, None)                        # FreeD path: TC injected
            self.assertEqual(len(rx.recvfrom(100)[0]), 33)
        finally:
            fwd.close()
            rx.close()

    def test_new_config_fields_round_trip(self):
        path = os.path.join(self.tmp, 'c.json')
        fwd = self.fr.FreeDForwarder(config_path=path)
        self.assertEqual(fwd.input_mode, 'freed')
        self.assertEqual(fwd.oti_listen_port, 45000)
        fwd.input_mode, fwd.oti_listen_port = 'both', 46000
        fwd.active_source, fwd.oti_sender_ip = 'oti', '10.78.30.105'
        fwd.save_config()
        fwd2 = self.fr.FreeDForwarder(config_path=path)
        self.assertEqual((fwd2.input_mode, fwd2.oti_listen_port, fwd2.active_source, fwd2.oti_sender_ip),
                         ('both', 46000, 'oti', '10.78.30.105'))
        fwd.close()
        fwd2.close()

    def test_unknown_input_mode_falls_back_to_freed(self):
        path = os.path.join(self.tmp, 'c.json')
        with open(path, 'w') as fh:
            json.dump({'input_mode': 'bogus'}, fh)
        fwd = self.fr.FreeDForwarder(config_path=path)
        self.assertEqual(fwd.input_mode, 'freed')
        fwd.close()


if __name__ == '__main__':
    unittest.main()
