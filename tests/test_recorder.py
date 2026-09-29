#!/usr/bin/env python3
"""Tests for the recorder / analyzer: capture, .fdrec round trip, field classification."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.recorder import (Recorder, Recording, analyze, report_csv_rows,
                          STATUS_LIVE, STATUS_FIXED, STATUS_ZERO, STATUS_MISSING)
from src.protocol import PROTO_OTI, PROTO_FREED
from tests.test_opentrackio_input import load_sample, build_segments
from tests.test_freed import build_freed_packet

try:
    import cbor2
except ImportError:
    cbor2 = None

SRC_A = ('10.78.30.105', 46001)
SRC_B = ('10.78.10.213', 48609)


def oti_datagrams(n=24, sample=None, encoding=0x01, zoom_live=True):
    """n samples at 24 Hz from one sender, as (t, addr, port, data) rows."""
    smp = sample or load_sample()
    rows = []
    for i in range(n):
        s = json.loads(json.dumps(smp))
        if zoom_live:
            s['lens']['pinholeFocalLength'] = 28.0 + i
        body = cbor2.dumps(s) if encoding == 0x02 else json.dumps(s, separators=(',', ':')).encode()
        for d in build_segments(body, seq=i, encoding=encoding):
            rows.append((i / 24.0, SRC_A, 5000, d))
    return rows


def fields_by_path(report):
    return {f['path']: f for f in report['fields']}


class TestRecorder(unittest.TestCase):

    def test_records_only_within_duration(self):
        r = Recorder()
        r.start(2.0, now=100.0, ports=[5000])
        r.add(b'a', SRC_A, 5000, 100.5)
        r.add(b'b', SRC_A, 5000, 101.9)
        r.add(b'c', SRC_A, 5000, 102.1)          # past the end — stops recording
        r.add(b'd', SRC_A, 5000, 102.2)
        self.assertFalse(r.active)
        self.assertEqual([d[3] for d in r.recording().datagrams], [b'a', b'b'])
        self.assertAlmostEqual(r.recording().datagrams[0][0], 0.5)

    def test_poll_stops_without_traffic(self):
        r = Recorder()
        r.start(1.0, now=0.0)
        self.assertTrue(r.poll(0.5))
        self.assertFalse(r.poll(1.0))

    def test_inactive_recorder_ignores_datagrams(self):
        r = Recorder()
        r.add(b'x', SRC_A, 5000, 1.0)
        self.assertEqual(r.count, 0)


class TestRecordingFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_keeps_raw_bytes(self):
        rows = oti_datagrams(5)
        rec = Recording(rows, {'duration': 0.2, 'created': '2026-09-29T12:00:00'})
        path = os.path.join(self.tmp, 'a.fdrec')
        rec.save(path)
        back = Recording.load(path)
        self.assertEqual([d[3] for d in back.datagrams], [d[3] for d in rows])
        self.assertEqual(back.datagrams[0][1], SRC_A)
        self.assertEqual(back.meta['ports'], [5000])
        self.assertEqual(back.meta['format'], 'fdrec')

    def test_rejects_other_files(self):
        path = os.path.join(self.tmp, 'x.fdrec')
        with open(path, 'w') as fh:
            fh.write('{"hello": 1}\n')
        with self.assertRaises(ValueError):
            Recording.load(path)

    def test_reports_damaged_line(self):
        path = os.path.join(self.tmp, 'x.fdrec')
        with open(path, 'w') as fh:
            fh.write('{"format": "fdrec", "version": 1}\n{"t": 0, "src": "1.2.3.4:5"}\n')
        with self.assertRaisesRegex(ValueError, 'line 2'):
            Recording.load(path)


class TestAnalyzeOTI(unittest.TestCase):

    def setUp(self):
        self.rep = analyze(Recording(oti_datagrams(24)))[0]
        self.f = fields_by_path(self.rep)

    def test_stream_summary(self):
        self.assertEqual(self.rep['proto'], PROTO_OTI)
        self.assertEqual(self.rep['samples'], 24)
        self.assertEqual(self.rep['datagrams'], 48)           # 2 segments per sample
        self.assertAlmostEqual(self.rep['rate'], 24.0, places=3)
        self.assertAlmostEqual(self.rep['interval']['mean_ms'], 1000 / 24, places=3)
        self.assertIn('ASR-CT1', self.rep['label'])
        self.assertEqual(self.rep['errors']['Lost samples'], 0)

    def test_classification(self):
        self.assertEqual(self.f['lens.pinholeFocalLength']['status'], STATUS_LIVE)
        self.assertEqual(self.f['lens.pinholeFocalLength']['summary'], '28 … 51')
        self.assertEqual(self.f['lens.distortion[0].radial[0]']['status'], STATUS_ZERO)
        self.assertEqual(self.f['lens.entrancePupilOffset']['status'], STATUS_ZERO)
        self.assertEqual(self.f['static.lens.make']['status'], STATUS_ZERO)       # 'N/A'
        self.assertEqual(self.f['static.camera.model']['status'], STATUS_FIXED)
        self.assertEqual(self.f['tracker.recording']['status'], STATUS_FIXED)     # False is a value
        self.assertEqual(self.f['lens.tStop']['status'], STATUS_MISSING)
        self.assertEqual(self.f['sourceId']['status'], STATUS_MISSING)
        self.assertNotIn('timing.synchronization', self.f)                       # present below it
        self.assertEqual(self.f['sampleId']['status'], STATUS_LIVE)

    def test_counts_match_fields(self):
        self.assertEqual(sum(self.rep['counts'].values()), len(self.rep['fields']))

    def test_csv_rows(self):
        rows = report_csv_rows(self.rep)
        self.assertEqual(rows[0], ['field', 'status', 'value / range', 'distinct values'])
        self.assertEqual(len(rows), len(self.rep['fields']) + 1)

    @unittest.skipIf(cbor2 is None, 'cbor2 not installed')
    def test_cbor_stream_gives_same_fields(self):
        cbor_rep = analyze(Recording(oti_datagrams(24, encoding=0x02)))[0]
        self.assertEqual(cbor_rep['errors']['Decode errors'], 0)
        self.assertEqual({f['path']: f['status'] for f in cbor_rep['fields'] if f['path'] != 'sampleId'},
                         {f['path']: f['status'] for f in self.rep['fields'] if f['path'] != 'sampleId'})
        self.assertTrue(any('CBOR' in i for i in cbor_rep['info']))


class TestAnalyzeMixed(unittest.TestCase):

    def test_streams_split_by_sender_and_protocol(self):
        rows = oti_datagrams(10)
        for i in range(30):
            pkt = build_freed_packet(2, 10.0 + i, 0, 0, 1.0, 2.0, 0.5, 35.0, False, 2.0, False, True, i)
            rows.append((i / 50.0, SRC_B, 40000, pkt))
        rows.append((0.1, SRC_B, 40000, b'not tracking data at all'))
        reps = analyze(Recording(sorted(rows, key=lambda r: r[0])))
        protos = [r['proto'] for r in reps]
        self.assertEqual(protos, [PROTO_FREED, PROTO_OTI, None])        # busiest first
        freed = reps[0]
        self.assertEqual(freed['samples'], 30)
        self.assertEqual(freed['errors']['Checksum errors'], 0)
        self.assertIn('standard ×30', freed['info'][0])
        f = fields_by_path(freed)
        self.assertEqual(f['pan_deg']['status'], STATUS_LIVE)
        self.assertEqual(f['camera_id']['summary'], '2')
        self.assertEqual(f['ext_timecode']['status'], STATUS_MISSING)
        self.assertEqual(reps[2]['samples'], 0)

    def test_empty_recording(self):
        self.assertEqual(analyze(Recording([])), [])


if __name__ == '__main__':
    unittest.main()
