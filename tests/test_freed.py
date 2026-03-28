#!/usr/bin/env python3
"""Comprehensive unit tests for FreeD protocol components."""
import sys
import os
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import FreeDParser, FreeDReceiver, FreeDReceiverGUI
from opentrackio import OpenTrackIOSender


# ---------------------------------------------------------------------------
# Helpers mirroring freed_simulator functions (avoid PyQt6 import at top level)
# ---------------------------------------------------------------------------

def _pack_24bit_signed(value: int) -> bytes:
    """Pack a signed integer into 3 bytes big-endian."""
    value = max(-8388608, min(8388607, value))
    if value < 0:
        value += 0x1000000
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def build_freed_packet(
    camera_id: int,
    pan_deg: float, tilt_deg: float, roll_deg: float,
    x_m: float, y_m: float, z_m: float,
    zoom_mm: float, zoom_no_data: bool,
    focus_m: float, focus_no_data: bool,
    genlock_on: bool, phase_counter: int,
) -> bytes:
    """Build a 29-byte FreeD D1 packet."""
    pan_raw   = int(pan_deg  * 32768)
    tilt_raw  = int(tilt_deg * 32768)
    roll_raw  = int(roll_deg * 32768)
    x_raw     = int(x_m * 64000)
    y_raw     = int(y_m * 64000)
    z_raw     = int(z_m * 64000)
    zoom_raw  = 0 if zoom_no_data else max(0, int(zoom_mm * 1000))
    focus_raw = 65535 if focus_no_data else max(0, int(focus_m * 1000))

    pkt = bytearray(29)
    pkt[0]  = 0xD1
    pkt[1]  = camera_id & 0xFF
    pkt[2:5]   = _pack_24bit_signed(pan_raw)
    pkt[5:8]   = _pack_24bit_signed(tilt_raw)
    pkt[8:11]  = _pack_24bit_signed(roll_raw)
    pkt[11:14] = _pack_24bit_signed(x_raw)
    pkt[14:17] = _pack_24bit_signed(y_raw)
    pkt[17:20] = _pack_24bit_signed(z_raw)
    pkt[20:23] = _pack_24bit_signed(zoom_raw)
    pkt[23:26] = _pack_24bit_signed(focus_raw)

    if genlock_on:
        pkt[26] = (phase_counter & 0x0F) << 4
    else:
        pkt[26] = 0x00
    pkt[27] = 0x00

    # Device checksum: (byte26 + byte27 + byte28) & 0xFF == 0xF6
    pkt[28] = (0xF6 - pkt[26] - pkt[27]) & 0xFF

    return bytes(pkt)


# ---------------------------------------------------------------------------
# 1. FreeDParser
# ---------------------------------------------------------------------------

class TestFreeDParser(unittest.TestCase):

    def setUp(self):
        self.parser = FreeDParser(ignore_checksum=True)

    def test_parse_24bit_int_positive(self):
        data = bytes([0x00, 0x01, 0x00])
        self.assertEqual(self.parser.parse_24bit_int(data), 256)

    def test_parse_24bit_int_negative(self):
        # 0xFF0000 as 24-bit signed = -65536
        data = bytes([0xFF, 0x00, 0x00])
        self.assertEqual(self.parser.parse_24bit_int(data), -65536)

    def test_parse_24bit_int_max(self):
        data = bytes([0x7F, 0xFF, 0xFF])
        self.assertEqual(self.parser.parse_24bit_int(data), 8388607)

    def test_parse_24bit_int_min(self):
        data = bytes([0x80, 0x00, 0x00])
        self.assertEqual(self.parser.parse_24bit_int(data), -8388608)

    def test_parse_24bit_int_zero(self):
        data = bytes([0x00, 0x00, 0x00])
        self.assertEqual(self.parser.parse_24bit_int(data), 0)

    def test_calculate_checksum_known_value(self):
        # calculate_checksum returns (0xF6 - byte26 - byte27) & 0xFF
        # For a 29-byte packet with byte26=0x00, byte27=0x00: expected = 0xF6
        pkt = bytearray(29)
        pkt[0] = 0xD1
        self.assertEqual(self.parser.calculate_checksum(bytes(pkt)), 0xF6)

    def test_calculate_checksum_known_value2(self):
        # byte26=0x31, byte27=0x20 → expected = (0xF6 - 0x31 - 0x20) & 0xFF = 0xA5
        pkt = bytearray(29)
        pkt[0] = 0xD1
        pkt[26] = 0x31
        pkt[27] = 0x20
        self.assertEqual(self.parser.calculate_checksum(bytes(pkt)), 0xA5)

    def test_verify_checksum_valid(self):
        pkt = bytearray(29)
        pkt[0] = 0xD1
        pkt[26] = 0x31; pkt[27] = 0x20; pkt[28] = 0xA5
        self.assertTrue(self.parser.verify_checksum(bytes(pkt)))

    def test_verify_checksum_invalid(self):
        pkt = bytearray(29)
        pkt[0] = 0xD1
        pkt[26] = 0x31; pkt[27] = 0x20; pkt[28] = 0x00  # wrong
        self.assertFalse(self.parser.verify_checksum(bytes(pkt)))

    def _make_valid_packet(self, camera_id=1):
        """Build a minimal valid 29-byte FreeD D1 packet."""
        return build_freed_packet(
            camera_id=camera_id,
            pan_deg=0.0, tilt_deg=0.0, roll_deg=0.0,
            x_m=0.0, y_m=0.0, z_m=0.0,
            zoom_mm=50.0, zoom_no_data=False,
            focus_m=1.0, focus_no_data=False,
            genlock_on=False, phase_counter=0,
        )

    def test_parse_valid_packet(self):
        pkt = self._make_valid_packet(camera_id=3)
        result = self.parser.parse(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result['camera_id'], 3)
        self.assertTrue(result['checksum_valid'])

    def test_parse_too_small(self):
        result = self.parser.parse(bytes(10))
        self.assertIsNone(result)

    def test_parse_wrong_message_type(self):
        pkt = bytearray(self._make_valid_packet())
        pkt[0] = 0xD2
        result = self.parser.parse(bytes(pkt))
        self.assertIsNone(result)

    def test_parse_checksum_mismatch_ignored(self):
        pkt = bytearray(self._make_valid_packet())
        pkt[28] ^= 0xFF  # corrupt checksum
        parser = FreeDParser(ignore_checksum=True)
        result = parser.parse(bytes(pkt))
        self.assertIsNotNone(result)
        self.assertFalse(result['checksum_valid'])

    def test_parse_checksum_mismatch_counted(self):
        pkt = bytearray(self._make_valid_packet())
        pkt[28] ^= 0xFF  # corrupt checksum
        parser = FreeDParser(ignore_checksum=False)
        before = parser.error_count
        result = parser.parse(bytes(pkt))
        # Still parsed (returns data) but error_count incremented
        self.assertIsNotNone(result)
        self.assertEqual(parser.error_count, before + 1)

    def test_parse_extended_tc_block(self):
        pkt = bytearray(self._make_valid_packet())
        # Append 4-byte extended TC block: H=10, M=30, S=45, F=12
        pkt += bytearray([10, 30, 45, 12])
        result = self.parser.parse(bytes(pkt))
        self.assertIsNotNone(result)
        self.assertEqual(result['ext_tc'], (10, 30, 45, 12))

    def test_parse_extended_checksum_correct(self):
        """Extended packet checksum is validated against bytes 0-27 only."""
        pkt = bytearray(self._make_valid_packet())
        pkt += bytearray([1, 2, 3, 4])  # extended TC
        result = self.parser.parse(bytes(pkt))
        self.assertIsNotNone(result)
        self.assertTrue(result['checksum_valid'])


# ---------------------------------------------------------------------------
# 2. FreeDReceiver interpolation
# ---------------------------------------------------------------------------

class TestFreeDReceiverInterpolation(unittest.TestCase):

    def setUp(self):
        self.recv = FreeDReceiver(port=59999)

    # Zoom
    def test_interpolate_zoom_exact_points(self):
        for raw, expected in self.recv.zoom_calibration:
            self.assertAlmostEqual(self.recv.interpolate_zoom(raw), expected, places=6)

    def test_interpolate_zoom_below_min(self):
        min_raw, min_val = self.recv.zoom_calibration[0]
        self.assertEqual(self.recv.interpolate_zoom(min_raw - 1000), min_val)

    def test_interpolate_zoom_above_max(self):
        max_raw, max_val = self.recv.zoom_calibration[-1]
        self.assertEqual(self.recv.interpolate_zoom(max_raw + 1000), max_val)

    def test_interpolate_zoom_midpoint(self):
        raw0, val0 = self.recv.zoom_calibration[0]
        raw1, val1 = self.recv.zoom_calibration[1]
        mid_raw = (raw0 + raw1) / 2.0
        mid_val = (val0 + val1) / 2.0
        self.assertAlmostEqual(self.recv.interpolate_zoom(mid_raw), mid_val, places=6)

    # Focus
    def test_interpolate_focus_exact_points(self):
        for raw, expected in self.recv.focus_calibration:
            self.assertAlmostEqual(self.recv.interpolate_focus(raw), expected, places=6)

    def test_interpolate_focus_below_min(self):
        min_raw, min_val = self.recv.focus_calibration[0]
        self.assertEqual(self.recv.interpolate_focus(min_raw - 100), min_val)

    def test_interpolate_focus_above_max(self):
        max_raw, max_val = self.recv.focus_calibration[-1]
        self.assertEqual(self.recv.interpolate_focus(max_raw + 1000), max_val)

    def test_interpolate_focus_midpoint(self):
        raw0, val0 = self.recv.focus_calibration[0]
        raw1, val1 = self.recv.focus_calibration[1]
        mid_raw = (raw0 + raw1) / 2.0
        mid_val = (val0 + val1) / 2.0
        self.assertAlmostEqual(self.recv.interpolate_focus(mid_raw), mid_val, places=6)

    # Timecode
    def test_parse_timecode_basic(self):
        # H=1, M=2, S=4  (seconds must be even due to /2 encoding)
        # bits [15:11]=1, [10:5]=2, [4:0]=4/2=2
        wire = ((1 & 0x1F) << 11) | ((2 & 0x3F) << 5) | ((4 >> 1) & 0x1F)
        tc = self.recv.parse_timecode(wire, 25.0)
        self.assertEqual(tc, '01:02:04:00')

    def test_parse_timecode_none_fps(self):
        tc = self.recv.parse_timecode(0, None)
        self.assertIsNone(tc)

    def test_parse_timecode_zero_fps(self):
        tc = self.recv.parse_timecode(0, 0)
        self.assertIsNone(tc)


# ---------------------------------------------------------------------------
# 3. Build FreeD packet helpers
# ---------------------------------------------------------------------------

class TestBuildFreeDPacket(unittest.TestCase):

    def _pkt(self, **kwargs):
        defaults = dict(
            camera_id=1,
            pan_deg=0.0, tilt_deg=0.0, roll_deg=0.0,
            x_m=0.0, y_m=0.0, z_m=0.0,
            zoom_mm=50.0, zoom_no_data=False,
            focus_m=1.0, focus_no_data=False,
            genlock_on=False, phase_counter=0,
        )
        defaults.update(kwargs)
        return build_freed_packet(**defaults)

    def test_packet_is_29_bytes(self):
        self.assertEqual(len(self._pkt()), 29)

    def test_packet_starts_with_d1(self):
        self.assertEqual(self._pkt()[0], 0xD1)

    def test_packet_camera_id(self):
        pkt = self._pkt(camera_id=5)
        self.assertEqual(pkt[1], 5)

    def test_packet_checksum_valid(self):
        pkt = self._pkt()
        # Device checksum: (byte26 + byte27 + byte28) & 0xFF == 0xF6
        self.assertEqual((pkt[26] + pkt[27] + pkt[28]) & 0xFF, 0xF6)

    def test_packet_pan_encode_decode(self):
        pan_deg = 12.5
        pkt = self._pkt(pan_deg=pan_deg)
        raw = int.from_bytes(pkt[2:5], byteorder='big', signed=True)
        self.assertAlmostEqual(raw / 32768.0, pan_deg, places=3)

    def test_packet_tilt_encode_decode(self):
        tilt_deg = -7.0
        pkt = self._pkt(tilt_deg=tilt_deg)
        raw = int.from_bytes(pkt[5:8], byteorder='big', signed=True)
        self.assertAlmostEqual(raw / 32768.0, tilt_deg, places=3)

    def test_packet_roll_encode_decode(self):
        roll_deg = 3.25
        pkt = self._pkt(roll_deg=roll_deg)
        raw = int.from_bytes(pkt[8:11], byteorder='big', signed=True)
        self.assertAlmostEqual(raw / 32768.0, roll_deg, places=3)

    def test_packet_position_encode_decode(self):
        x_m, y_m, z_m = 1.5, -0.5, 2.0
        pkt = self._pkt(x_m=x_m, y_m=y_m, z_m=z_m)
        rx = int.from_bytes(pkt[11:14], byteorder='big', signed=True) / 64000.0
        ry = int.from_bytes(pkt[14:17], byteorder='big', signed=True) / 64000.0
        rz = int.from_bytes(pkt[17:20], byteorder='big', signed=True) / 64000.0
        self.assertAlmostEqual(rx, x_m, places=3)
        self.assertAlmostEqual(ry, y_m, places=3)
        self.assertAlmostEqual(rz, z_m, places=3)

    def test_packet_zoom_no_data(self):
        pkt = self._pkt(zoom_no_data=True)
        zoom_raw = int.from_bytes(pkt[20:23], byteorder='big', signed=True)
        self.assertEqual(zoom_raw, 0)

    def test_packet_focus_no_data(self):
        pkt = self._pkt(focus_no_data=True)
        focus_raw = int.from_bytes(pkt[23:26], byteorder='big', signed=True)
        self.assertEqual(focus_raw, 65535)

    def test_packet_genlock_off(self):
        pkt = self._pkt(genlock_on=False)
        self.assertEqual(pkt[26], 0x00)

    def test_packet_genlock_on_phase(self):
        pkt = self._pkt(genlock_on=True, phase_counter=7)
        self.assertEqual((pkt[26] >> 4) & 0x0F, 7)

    def test_build_and_parse_roundtrip(self):
        pan_deg = 45.0
        tilt_deg = -20.0
        pkt = self._pkt(
            camera_id=2, pan_deg=pan_deg, tilt_deg=tilt_deg,
            x_m=1.0, y_m=2.0, z_m=3.0,
        )
        parser = FreeDParser(ignore_checksum=False)
        result = parser.parse(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result['camera_id'], 2)
        self.assertAlmostEqual(result['pan'] / 32768.0, pan_deg, places=3)
        self.assertAlmostEqual(result['tilt'] / 32768.0, tilt_deg, places=3)
        self.assertTrue(result['checksum_valid'])


# ---------------------------------------------------------------------------
# 4. OpenTrackIO Fletcher-16
# ---------------------------------------------------------------------------

class TestOpenTrackIOFletch16(unittest.TestCase):

    def test_fletcher16_empty(self):
        result = OpenTrackIOSender._fletcher16(b'')
        self.assertEqual(result, 0)

    def test_fletcher16_known_value(self):
        # Manually compute: data = [0x01, 0x02]
        # s1 after 0x01 = 1, s2 = 1
        # s1 after 0x02 = 3, s2 = 4
        # result = (4 << 8) | 3 = 0x0403
        result = OpenTrackIOSender._fletcher16(bytes([0x01, 0x02]))
        self.assertEqual(result, 0x0403)

    def test_fletcher16_single_byte(self):
        # data = [0x42]: s1 = 0x42, s2 = 0x42 → result = (0x42 << 8) | 0x42
        result = OpenTrackIOSender._fletcher16(bytes([0x42]))
        self.assertEqual(result, (0x42 << 8) | 0x42)


# ---------------------------------------------------------------------------
# 5. OpenTrackIO sequence number
# ---------------------------------------------------------------------------

class TestOpenTrackIOSeq(unittest.TestCase):

    def _make_sender(self):
        sender = OpenTrackIOSender()
        sender.enabled = True
        # Replace socket with a mock
        sender._sock = MagicMock()
        sender._sock.sendto = MagicMock()
        return sender

    def test_seq_increments_once_per_send(self):
        sender = self._make_sender()
        sender._seq = 0
        data = {'pan': 0, 'tilt': 0, 'roll': 0, 'position': {}, 'focus': 0, 'zoom': 0}
        for expected in range(1, 6):
            sender.send(data)
            self.assertEqual(sender._seq, expected)

    def test_seq_wraps_at_65535(self):
        sender = self._make_sender()
        sender._seq = 65534
        data = {'pan': 0, 'tilt': 0, 'roll': 0, 'position': {}, 'focus': 0, 'zoom': 0}
        sender.send(data)
        self.assertEqual(sender._seq, 65535)
        sender.send(data)
        self.assertEqual(sender._seq, 0)


# ---------------------------------------------------------------------------
# Helper: import freed_reader with PyQt6 stubbed out
# ---------------------------------------------------------------------------

def _import_freed_reader():
    """Return the freed_reader module, stubbing out PyQt6 if not installed."""
    if 'freed_reader' in sys.modules:
        return sys.modules['freed_reader']
    # Stub every PyQt6 sub-module that freed_reader imports
    _qt_stub = MagicMock()
    _qt_stub.QFont = MagicMock
    qt_modules = [
        'PyQt6', 'PyQt6.QtWidgets', 'PyQt6.QtCore', 'PyQt6.QtGui',
        'pyqtgraph', 'numpy',
    ]
    saved = {}
    for mod in qt_modules:
        saved[mod] = sys.modules.get(mod)
        sys.modules[mod] = _qt_stub
    try:
        import freed_reader
    finally:
        for mod, orig in saved.items():
            if orig is None:
                sys.modules.pop(mod, None)
            else:
                sys.modules[mod] = orig
    return freed_reader


# ---------------------------------------------------------------------------
# 6. FreeDForwarder config
# ---------------------------------------------------------------------------

class TestFreeDForwarderConfig(unittest.TestCase):

    def _make_forwarder(self, config_path):
        freed_reader = _import_freed_reader()
        return freed_reader.FreeDForwarder(config_path=config_path)

    def test_load_missing_config_has_defaults(self):
        tmpdir = tempfile.mkdtemp()
        try:
            config_path = os.path.join(tmpdir, 'nonexistent_config.json')
            fwd = self._make_forwarder(config_path)
            self.assertEqual(fwd.tc_fps, 25.0)
            self.assertEqual(fwd.ltc_connector, 2)
            self.assertTrue(fwd.tc_inject)
            self.assertEqual(fwd.listen_port, 45000)
        finally:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_and_reload_config(self):
        tmpdir = tempfile.mkdtemp()
        try:
            config_path = os.path.join(tmpdir, 'test_config.json')
            fwd = self._make_forwarder(config_path)
            fwd.tc_fps = 29.97
            fwd.ltc_connector = 3
            fwd.listen_port = 50000
            fwd.oti_enabled = True
            fwd.save_config()

            fwd2 = self._make_forwarder(config_path)
            self.assertAlmostEqual(fwd2.tc_fps, 29.97, places=2)
            self.assertEqual(fwd2.ltc_connector, 3)
            self.assertEqual(fwd2.listen_port, 50000)
            self.assertTrue(fwd2.oti_enabled)
        finally:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. _inject_tc checksum
# ---------------------------------------------------------------------------

class TestInjectTCChecksum(unittest.TestCase):

    def _make_forwarder(self, config_path):
        freed_reader = _import_freed_reader()
        fwd = freed_reader.FreeDForwarder(config_path=config_path)
        fwd.tc_inject = True
        fwd.tc_source = 'system'
        fwd.tc_fps = 25.0
        return fwd

    def test_inject_tc_checksum_is_valid(self):
        tmpdir = tempfile.mkdtemp()
        try:
            config_path = os.path.join(tmpdir, 'cfg.json')
            fwd = self._make_forwarder(config_path)
            raw_pkt = build_freed_packet(
                camera_id=1,
                pan_deg=10.0, tilt_deg=5.0, roll_deg=0.0,
                x_m=0.0, y_m=0.0, z_m=0.0,
                zoom_mm=35.0, zoom_no_data=False,
                focus_m=2.0, focus_no_data=False,
                genlock_on=False, phase_counter=0,
            )
            result = fwd._inject_tc(bytearray(raw_pkt), ltc_reader=None)
            # Verify checksum: (byte26 + byte27 + byte28) & 0xFF == 0xF6
            self.assertEqual((result[26] + result[27] + result[28]) & 0xFF, 0xF6)
        finally:
            import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
