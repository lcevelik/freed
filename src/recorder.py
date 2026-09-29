"""
Recorder & analyzer — capture the raw UDP datagrams arriving on the listening
ports, save / load them, and report which fields each stream actually carries.

Recording file (.fdrec) — UTF-8 JSON Lines:
  line 1   {"format": "fdrec", "version": 1, "app": ..., "created": ISO-8601,
            "duration": seconds, "ports": [..]}
  then     {"t": seconds from start, "src": "ip:port", "port": local port,
            "data": base64 of the raw datagram}
Raw datagrams are kept (not decoded values) so a recording can be re-analysed
with any future parser, including CBOR streams.
"""

import base64
import json
import math
import threading
from datetime import datetime

from .protocol import FreeDParser, detect_protocol, PROTO_FREED, PROTO_OTI, PROTO_NAMES
from .opentrackio import OpenTrackIOParser, oti_device_label

FORMAT    = 'fdrec'
VERSION   = 1
EXTENSION = '.fdrec'

STATUS_LIVE    = 'live'      # more than one value seen
STATUS_FIXED   = 'fixed'     # one meaningful value
STATUS_ZERO    = 'zero'      # present, but always 0 / 'N/A' / empty
STATUS_MISSING = 'missing'   # reference field never present
STATUS_NAMES   = {STATUS_LIVE: 'Live', STATUS_FIXED: 'Fixed',
                  STATUS_ZERO: 'Zero', STATUS_MISSING: 'Not sent'}

# OpenTrackIO fields worth reporting as "Not sent" when absent. A field counts
# as present if the path itself or anything below it appears in the stream.
OTI_REFERENCE_FIELDS = [
    'protocol', 'sampleId', 'sourceId', 'sourceNumber',
    'static.camera.make', 'static.camera.model', 'static.camera.serialNumber',
    'static.camera.firmwareVersion', 'static.camera.label',
    'static.camera.activeSensorPhysicalDimensions', 'static.camera.activeSensorResolution',
    'static.camera.captureFrameRate', 'static.camera.isoSpeed', 'static.camera.shutterAngle',
    'static.lens.make', 'static.lens.model', 'static.lens.serialNumber',
    'static.lens.firmwareVersion', 'static.lens.nominalFocalLength',
    'static.tracker.make', 'static.tracker.model', 'static.tracker.serialNumber',
    'static.tracker.firmwareVersion',
    'tracker.status', 'tracker.recording', 'tracker.slate',
    'timing.sampleRate', 'timing.timecode', 'timing.sequenceNumber', 'timing.synchronization',
    'transforms',
    'lens.pinholeFocalLength', 'lens.focusDistance', 'lens.fStop', 'lens.tStop',
    'lens.entrancePupilOffset', 'lens.encoders', 'lens.rawEncoders',
    'lens.distortion', 'lens.distortionOverscan', 'lens.distortionOffset', 'lens.projectionOffset',
]

_ZERO_VALUES = (0, 0.0, '', 'N/A', 'n/a', 'NA', None)
_MAX_DISTINCT = 5000


# ══════════════════════════════════════════════════════════════════════════════
# Capture
# ══════════════════════════════════════════════════════════════════════════════

class Recording:
    """An immutable-ish list of (t, addr, port, data) plus metadata."""

    def __init__(self, datagrams=None, meta=None):
        self.datagrams = datagrams or []          # [(t, (ip, port), local_port, bytes)]
        self.meta = dict(meta or {})

    @property
    def duration(self) -> float:
        return float(self.meta.get('duration') or
                     (self.datagrams[-1][0] if self.datagrams else 0.0))

    def save(self, path: str):
        meta = dict(self.meta)
        meta.update(format=FORMAT, version=VERSION)
        meta.setdefault('ports', sorted({d[2] for d in self.datagrams}))
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(meta) + '\n')
            for t, addr, port, data in self.datagrams:
                fh.write(json.dumps({'t': round(t, 6), 'src': f'{addr[0]}:{addr[1]}', 'port': port,
                                     'data': base64.b64encode(data).decode('ascii')}) + '\n')

    @classmethod
    def load(cls, path: str) -> 'Recording':
        with open(path, encoding='utf-8') as fh:
            first = fh.readline()
            try:
                meta = json.loads(first)
            except ValueError:
                raise ValueError('Not a FreeD Dashboard recording (first line is not JSON).')
            if meta.get('format') != FORMAT:
                raise ValueError('Not a FreeD Dashboard recording (missing "fdrec" header).')
            if int(meta.get('version', 0)) > VERSION:
                raise ValueError(f"Recording format v{meta.get('version')} is newer than this app supports.")
            datagrams = []
            for n, line in enumerate(fh, start=2):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    ip, _, sport = row['src'].rpartition(':')
                    datagrams.append((float(row['t']), (ip, int(sport)), int(row['port']),
                                      base64.b64decode(row['data'])))
                except (ValueError, KeyError) as e:
                    raise ValueError(f'Recording is damaged at line {n}: {e}')
        return cls(datagrams, meta)


class Recorder:
    """
    Thread-safe in-memory capture. Receive threads call add() for every
    datagram; recording stops by itself once `duration` seconds have passed
    (checked on each datagram and by poll() from the UI timer).
    """

    MAX_DATAGRAMS = 200000        # hard cap (~16 min of a 2-segment 24 Hz stream)

    def __init__(self):
        self._lock = threading.Lock()
        self._datagrams = []
        self.active = False
        self.t0 = None
        self.duration = 0.0
        self.started_at = None
        self.ports = []

    def start(self, duration: float, now: float, ports=()):
        with self._lock:
            self._datagrams = []
            self.t0 = now
            self.duration = float(duration)
            self.started_at = datetime.now()
            self.ports = sorted(ports)
            self.active = True

    def add(self, data: bytes, addr, port: int, t: float):
        if not self.active:
            return
        dt = t - self.t0
        if dt >= self.duration:
            self.active = False
            return
        with self._lock:
            if len(self._datagrams) < self.MAX_DATAGRAMS:
                self._datagrams.append((dt, addr, port, bytes(data)))

    def poll(self, now: float) -> bool:
        """Stop once the time is up. Returns True while still recording."""
        if self.active and now - self.t0 >= self.duration:
            self.active = False
        return self.active

    def stop(self):
        self.active = False

    def elapsed(self, now: float) -> float:
        return min(now - self.t0, self.duration) if self.t0 is not None else 0.0

    @property
    def count(self) -> int:
        return len(self._datagrams)

    def recording(self, actual_duration: float = None) -> Recording:
        with self._lock:
            dgs = list(self._datagrams)
        return Recording(dgs, {
            'created':  self.started_at.isoformat(timespec='seconds') if self.started_at else None,
            'duration': round(actual_duration if actual_duration is not None else self.duration, 3),
            'ports':    self.ports,
        })


# ══════════════════════════════════════════════════════════════════════════════
# Analysis
# ══════════════════════════════════════════════════════════════════════════════

class _FieldStats:
    __slots__ = ('values', 'count', 'num_min', 'num_max', 'overflow')

    def __init__(self):
        self.values = set()
        self.count = 0
        self.num_min = None
        self.num_max = None
        self.overflow = False

    def add(self, v):
        self.count += 1
        if isinstance(v, (int, float)) and not isinstance(v, bool) and not (
                isinstance(v, float) and math.isnan(v)):
            self.num_min = v if self.num_min is None else min(self.num_min, v)
            self.num_max = v if self.num_max is None else max(self.num_max, v)
        if len(self.values) < _MAX_DISTINCT:
            self.values.add(v if not isinstance(v, (list, dict)) else json.dumps(v, sort_keys=True))
        else:
            self.overflow = True


def _walk(obj, path, out: dict):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f'{path}.{k}' if path else k, out)
    elif isinstance(obj, list):
        if not obj:
            out.setdefault(path, _FieldStats()).add('[]')
        for i, v in enumerate(obj):
            _walk(v, f'{path}[{i}]', out)
    else:
        out.setdefault(path, _FieldStats()).add(obj)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f'{v:.6g}'
    return repr(v) if isinstance(v, str) else str(v)


def _classify(path: str, st: _FieldStats) -> dict:
    distinct = len(st.values)
    if distinct > 1 or st.overflow:
        status = STATUS_LIVE
        if st.num_min is not None and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in st.values):
            summary = f'{_fmt(st.num_min)} … {_fmt(st.num_max)}'
        else:
            sample = sorted(st.values, key=str)[:3]
            summary = ', '.join(_fmt(x) for x in sample) + (' …' if distinct > 3 else '')
    else:
        v = next(iter(st.values))
        status = STATUS_ZERO if (not isinstance(v, bool) and v in _ZERO_VALUES) else STATUS_FIXED
        summary = _fmt(v)
    return {'path': path, 'group': _group(path), 'status': status, 'summary': summary,
            'distinct': (f'{_MAX_DISTINCT}+' if st.overflow else distinct), 'count': st.count}


def _group(path: str) -> str:
    head = path.split('.')
    if head[0] == 'static' and len(head) > 1:
        return f'static.{head[1]}'
    return head[0].split('[')[0]


def _interval_stats(times):
    if len(times) < 2:
        return None
    iv = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
    mean = sum(iv) / len(iv)
    std = math.sqrt(sum((x - mean) ** 2 for x in iv) / len(iv))
    return {'mean_ms': mean, 'std_ms': std, 'min_ms': min(iv), 'max_ms': max(iv)}


def analyze(rec: Recording) -> list:
    """
    One report per (protocol, sender). Each report:
      proto, src, port, label, datagrams, samples, rate, interval (ms stats or None),
      errors {…}, info [str], fields [ {path, group, status, summary, distinct, count} ]
    Sorted with the busiest stream first.
    """
    streams = {}
    for t, addr, port, data in rec.datagrams:
        proto = detect_protocol(data)
        streams.setdefault((proto, addr, port), []).append((t, data))

    reports = []
    for (proto, addr, port), rows in streams.items():
        if proto == PROTO_OTI:
            reports.append(_analyze_oti(addr, port, rows))
        elif proto == PROTO_FREED:
            reports.append(_analyze_freed(addr, port, rows))
        else:
            reports.append(_report(proto, addr, port, rows, samples=0, times=[],
                                   label='Unrecognised data', fields=[],
                                   info=[f'First bytes: {rows[0][1][:8].hex(" ")}']))
    reports.sort(key=lambda r: -r['samples'])
    return reports


def _report(proto, addr, port, rows, samples, times, label, fields, errors=None, info=None):
    span = (times[-1] - times[0]) if len(times) > 1 else 0.0
    return {
        'proto': proto, 'proto_name': PROTO_NAMES.get(proto, 'Unknown'),
        'src': f'{addr[0]}:{addr[1]}', 'port': port, 'label': label,
        'datagrams': len(rows), 'samples': samples,
        'rate': ((samples - 1) / span) if span > 0 else None,
        'interval': _interval_stats(times),
        'errors': errors or {}, 'info': info or [], 'fields': fields,
        'counts': {s: sum(1 for f in fields if f['status'] == s) for s in STATUS_NAMES},
    }


def _analyze_oti(addr, port, rows) -> dict:
    parser = OpenTrackIOParser()
    stats, times, encodings, label = {}, [], {}, 'OpenTrackIO'
    for t, data in rows:
        smp = parser.feed(data, addr, t)
        if smp is None:
            continue
        meta = smp.pop('_meta', {})
        encodings[meta.get('encoding')] = encodings.get(meta.get('encoding'), 0) + 1
        times.append(t)
        label = oti_device_label(smp)
        _walk(smp, '', stats)

    fields = [_classify(p, st) for p, st in stats.items() if p != 'sampleId']
    if 'sampleId' in stats:
        fields.append({'path': 'sampleId', 'group': 'sampleId', 'status': STATUS_LIVE,
                       'summary': 'unique per sample', 'distinct': len(stats['sampleId'].values),
                       'count': stats['sampleId'].count})
    seen = list(stats)
    for ref in OTI_REFERENCE_FIELDS:
        if not any(p == ref or p.startswith(ref + '.') or p.startswith(ref + '[') for p in seen):
            fields.append({'path': ref, 'group': _group(ref), 'status': STATUS_MISSING,
                           'summary': '', 'distinct': 0, 'count': 0})
    fields.sort(key=lambda f: (_GROUP_ORDER.get(f['group'], 99), f['path']))

    info = []
    if encodings:
        info.append('Encoding: ' + ', '.join(f'{k} ×{v}' for k, v in encodings.items()))
    if parser.segment_count and times:
        info.append(f'{parser.segment_count / len(times):.1f} datagrams per sample')
    return _report(PROTO_OTI, addr, port, rows, samples=len(times), times=times, label=label,
                   fields=fields, info=info, errors={
                       'Checksum errors': parser.checksum_errors,
                       'Lost samples': parser.seq_gaps,
                       'Incomplete': parser.incomplete_count,
                       'Decode errors': parser.decode_errors,
                       **({'Last error': parser.last_error} if parser.last_error else {}),
                   })


def _analyze_freed(addr, port, rows) -> dict:
    parser = FreeDParser(ignore_checksum=True)
    stats, times, schemes, sizes, cams = {}, [], {}, {}, set()
    for t, data in rows:
        d = parser.parse(data)
        if d is None:
            continue
        times.append(t)
        schemes[d['checksum_scheme'] or 'mismatch'] = schemes.get(d['checksum_scheme'] or 'mismatch', 0) + 1
        sizes[len(data)] = sizes.get(len(data), 0) + 1
        cams.add(d['camera_id'])
        pos = d['position']
        _walk({
            'camera_id': d['camera_id'],
            'pan_deg':   round(d['pan'] / 32768.0, 5),
            'tilt_deg':  round(d['tilt'] / 32768.0, 5),
            'roll_deg':  round(d['roll'] / 32768.0, 5),
            'x_m':       round(pos['x'] / 64000.0, 6),
            'y_m':       round(pos['y'] / 64000.0, 6),
            'z_m':       round(pos['z'] / 64000.0, 6),
            'zoom_raw':  d['zoom'],
            'focus_raw': d['focus'],
            'spare_26_27': f'{d["spare"]:04X}',
            'ext_timecode': ':'.join(f'{v:02d}' for v in d['ext_tc']) if d['ext_tc'] else '',
        }, '', stats)

    fields = [_classify(p, st) for p, st in stats.items()]
    for f in fields:
        f['group'] = 'freed'
        if f['path'] == 'ext_timecode' and f['status'] == STATUS_ZERO:
            f['status'], f['summary'] = STATUS_MISSING, 'no bytes 29–32'
    bad = schemes.get('mismatch', 0)
    info = ['Checksum: ' + ', '.join(f'{k} ×{v}' for k, v in sorted(schemes.items())),
            'Packet size: ' + ', '.join(f'{k} B ×{v}' for k, v in sorted(sizes.items()))]
    return _report(PROTO_FREED, addr, port, rows, samples=len(times), times=times,
                   label='FreeD · cam ' + ', '.join(str(c) for c in sorted(cams)),
                   fields=fields, info=info, errors={'Checksum errors': bad})


_GROUP_ORDER = {'lens': 0, 'static.lens': 1, 'transforms': 2, 'timing': 3, 'static.camera': 4,
                'static.tracker': 5, 'tracker': 6, 'protocol': 7, 'sourceNumber': 8,
                'sourceId': 9, 'sampleId': 10}


def report_csv_rows(report: dict) -> list:
    """Rows for a CSV export of one stream report (header first)."""
    rows = [['field', 'status', 'value / range', 'distinct values']]
    for f in report['fields']:
        rows.append([f['path'], STATUS_NAMES[f['status']], f['summary'], f['distinct']])
    return rows
