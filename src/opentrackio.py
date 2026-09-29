"""
OpenTrackIO sender — converts FreeD data to OpenTrackIO v1.0.1 JSON over UDP.
Spec: SMPTE RIS-OSVP / opentrackio.org
"""

import json
import socket
import struct
import threading
import time
import uuid
from datetime import datetime


class OpenTrackIOSender:
    """
    Converts FreeD packets to OpenTrackIO v1.0.1 JSON over UDP.
    Spec: SMPTE RIS-OSVP / opentrackio.org
    Header: 17 bytes (magic + encoding + seq + segment + length + Fletcher-16)
    Payload: UTF-8 JSON
    Default transport: UDP unicast/multicast, port 55555
    """

    # FreeD unit scales (D1 protocol)
    # Pan/Tilt/Roll: 24-bit signed, 1 LSB = 1/32768 degree
    _ANGLE_SCALE = 1.0 / 32768.0
    # X/Y/Z: 24-bit signed, 1 LSB = 1/64 mm → divide by 64000 for metres
    _POS_SCALE   = 1.0 / 64000.0
    # Zoom/Focus: raw 24-bit, normalise 0-1
    _LENS_SCALE  = 1.0 / 16777215.0

    def __init__(self):
        self.enabled      = False
        self.ip           = '127.0.0.1'
        self.port         = 55555
        self.subject_name = 'Camera'
        self._seq         = 0
        self._source_id   = str(uuid.uuid4())   # stable device ID — overwritten from config
        self._lock       = threading.Lock()
        self._sock       = None
        self._open_socket()

    def _open_socket(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 5)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock = s
        except Exception:
            pass

    # ── public API ─────────────────────────────────────────────────────────

    def send(self, data: dict, ltc_reader=None, fps: float = 25.0):
        if not self.enabled or self._sock is None:
            return
        try:
            with self._lock:
                self._seq = (self._seq + 1) & 0xFFFF
                seq = self._seq
            payload = self._build_json(data, ltc_reader, fps, seq)
            packet  = self._build_packet(payload, seq)
            self._sock.sendto(packet, (self.ip, self.port))
        except Exception:
            pass

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    # ── internals ──────────────────────────────────────────────────────────

    def _build_json(self, data: dict, ltc_reader, fps: float, seq: int) -> bytes:
        fps_int = max(1, round(fps))
        # Timecode source
        if ltc_reader and ltc_reader.available:
            h, m, s, f, valid = ltc_reader.get()
            if not valid:
                h, m, s, f = self._system_tc(fps_int)
        else:
            h, m, s, f = self._system_tc(fps_int)

        pos = data.get('position', {})
        lens = {
            'encoders': {
                'focus': round(max(0.0, min(1.0,
                    data.get('focus', 0) * self._LENS_SCALE)), 6),
                'zoom':  round(max(0.0, min(1.0,
                    data.get('zoom',  0) * self._LENS_SCALE)), 6),
            },
        }
        if 'focal_length_mm' in data:
            fl = round(data['focal_length_mm'], 3)
            lens['focalLength']        = fl   # UE Live Link field name
            lens['pinholeFocalLength'] = fl   # OpenTrackIO spec field name
        if 'focus_distance_m' in data:
            lens['focusDistance'] = round(data['focus_distance_m'], 4)  # metres per spec

        payload = {
            'protocol':    {'name': 'OpenTrackIO', 'version': [1, 0, 1]},
            'sampleId':    f'urn:uuid:{uuid.uuid4()}',
            'sourceId':    f'urn:uuid:{self._source_id}',
            'sourceNumber': 1,
            'timing': {
                'mode':        'external',
                'sampleRate':  {'num': fps_int, 'denom': 1},
                'frameCount':  seq,
                'timecode': {
                    'hours':   h, 'minutes': m,
                    'seconds': s, 'frames':  f,
                    'frameRate': {'num': fps_int, 'denom': 1},
                },
            },
            'transforms': [{
                'id': self.subject_name,
                'translation': {
                    'x': round(pos.get('x', 0) * self._POS_SCALE, 6),
                    'y': round(pos.get('y', 0) * self._POS_SCALE, 6),
                    'z': round(pos.get('z', 0) * self._POS_SCALE, 6),
                },
                'rotation': {
                    'pan':  round(data.get('pan',  0) * self._ANGLE_SCALE, 6),
                    'tilt': round(data.get('tilt', 0) * self._ANGLE_SCALE, 6),
                    'roll': round(data.get('roll', 0) * self._ANGLE_SCALE, 6),
                },
            }],
            'lens': lens,
        }
        return json.dumps(payload, separators=(',', ':')).encode('utf-8')

    def _build_packet(self, payload: bytes, seq: int) -> bytes:
        n = len(payload)
        # 14-byte header before checksum (spec table, bits 0-111)
        hdr = bytearray(14)
        hdr[0:4] = b'OTrk'                        # 0-3: magic
        hdr[4]   = 0x00                            # 4: reserved
        hdr[5]   = 0x01                            # 5: JSON encoding
        struct.pack_into('>H', hdr, 6, seq)        # 6-7: sequence number
        struct.pack_into('>I', hdr, 8, 0)          # 8-11: segment offset = 0
        hdr[12] = 0x80 | ((n >> 8) & 0x7F)        # 12: last-seg(1) + len[14:8]
        hdr[13] = n & 0xFF                         # 13: len[7:0]
        # Bytes 14-15: Fletcher-16 over header[0:14] + payload
        ck = self._fletcher16(bytes(hdr) + payload)
        return bytes(hdr) + struct.pack('>H', ck) + payload

    @staticmethod
    def _fletcher16(data: bytes) -> int:
        """Fletcher-16 per OpenTrackIO spec (uint8 natural overflow, mod 256)."""
        s1 = s2 = 0
        for b in data:
            s1 = (s1 + b) & 0xFF
            s2 = (s2 + s1) & 0xFF
        return (s2 << 8) | s1

    @staticmethod
    def _system_tc(fps_int: int) -> tuple:
        now = datetime.now()
        f   = int((now.microsecond / 1_000_000) * fps_int)
        return now.hour, now.minute, now.second, f

    def relay(self, datagram: bytes):
        """Pass an incoming OpenTrackIO datagram (one segment) through unchanged."""
        if not self.enabled or self._sock is None:
            return
        try:
            self._sock.sendto(datagram, (self.ip, self.port))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Receive side
# ══════════════════════════════════════════════════════════════════════════════

OTI_MAGIC       = b'OTrk'
OTI_HEADER_SIZE = 16
OTI_ENC_JSON    = 0x01
OTI_ENC_CBOR    = 0x02


def fletcher16(data: bytes) -> int:
    """Fletcher-16 per OpenTrackIO spec (uint8 natural overflow, mod 256)."""
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) & 0xFF
        s2 = (s2 + s1) & 0xFF
    return (s2 << 8) | s1


def parse_oti_header(data: bytes):
    """
    Parse the 16-byte OpenTrackIO UDP header.
    Returns a dict, or None if the datagram is not an OpenTrackIO packet.
      0-3   'OTrk'
      4     reserved
      5     encoding (0x01 JSON, 0x02 CBOR)
      6-7   sequence number (uint16 BE)
      8-11  segment offset (uint32 BE)
      12-13 bit 15 = last segment, bits 14-0 = payload length
      14-15 Fletcher-16 over bytes 0-13 + payload
    """
    if len(data) < OTI_HEADER_SIZE or data[0:4] != OTI_MAGIC:
        return None
    seq, offset, len_field, checksum = struct.unpack('>HIHH', data[6:16])
    length  = len_field & 0x7FFF
    payload = data[OTI_HEADER_SIZE:OTI_HEADER_SIZE + length]
    return {
        'encoding':    data[5],
        'seq':         seq,
        'offset':      offset,
        'last':        bool(len_field & 0x8000),
        'length':      length,
        'checksum':    checksum,
        'checksum_ok': (len(payload) == length and
                        fletcher16(data[0:14] + payload) == checksum),
        'payload':     payload,
    }


class OpenTrackIOParser:
    """
    Reassembles segmented OpenTrackIO datagrams into complete samples.

    feed() is called from the receive thread with each datagram and returns
    a sample dict when the final segment completes a message, else None.
    The returned dict is the decoded payload plus a '_meta' entry.
    """

    PENDING_TIMEOUT = 0.5   # seconds before an incomplete message is dropped
    MAX_PENDING     = 32

    def __init__(self):
        self._pending = {}           # (addr, seq) -> {'t', 'segs': {offset: bytes}, 'total'}
        self._last_seq = {}          # addr -> last completed seq
        self.sample_count     = 0
        self.segment_count    = 0
        self.checksum_errors  = 0
        self.incomplete_count = 0    # messages dropped with segments missing
        self.decode_errors    = 0    # JSON errors / unsupported encoding
        self.seq_gaps         = 0    # samples lost according to sequence number
        self.last_error       = None

    def feed(self, data: bytes, addr=None, now: float = None):
        hdr = parse_oti_header(data)
        if hdr is None:
            return None
        self.segment_count += 1
        if not hdr['checksum_ok']:
            self.checksum_errors += 1
            return None
        now = time.perf_counter() if now is None else now
        self._expire(now)

        if hdr['offset'] == 0 and hdr['last']:
            body, n_segs = hdr['payload'], 1          # unsegmented — fast path
        else:
            key   = (addr, hdr['seq'])
            entry = self._pending.setdefault(key, {'t': now, 'segs': {}, 'total': None})
            entry['segs'][hdr['offset']] = hdr['payload']
            if hdr['last']:
                entry['total'] = hdr['offset'] + hdr['length']
            if entry['total'] is None:
                return None
            body = self._join(entry['segs'], entry['total'])
            if body is None:
                return None                           # still waiting on a middle segment
            n_segs = len(entry['segs'])
            del self._pending[key]

        sample = self._decode(hdr['encoding'], body)
        if sample is None:
            return None

        prev = self._last_seq.get(addr)
        if prev is not None:
            gap = (hdr['seq'] - prev - 1) & 0xFFFF
            if 0 < gap < 1000:
                self.seq_gaps += gap
        self._last_seq[addr] = hdr['seq']

        self.sample_count += 1
        sample['_meta'] = {
            'seq':          hdr['seq'],
            'encoding':     'JSON' if hdr['encoding'] == OTI_ENC_JSON else 'CBOR',
            'payload_size': len(body),
            'segments':     n_segs,
            'header':       bytes(data[:OTI_HEADER_SIZE]),
        }
        return sample

    @staticmethod
    def _join(segs: dict, total: int):
        out = bytearray()
        for off in sorted(segs):
            if off != len(out):
                return None
            out += segs[off]
        return bytes(out) if len(out) == total else None

    def _decode(self, encoding: int, body: bytes):
        try:
            if encoding == OTI_ENC_JSON:
                obj = json.loads(body.decode('utf-8'))
            elif encoding == OTI_ENC_CBOR:
                import cbor2   # optional dependency
                obj = cbor2.loads(body)
            else:
                raise ValueError(f'unknown encoding 0x{encoding:02X}')
            if not isinstance(obj, dict):
                raise ValueError('payload is not an object')
            return obj
        except Exception as e:
            self.decode_errors += 1
            self.last_error = str(e)
            return None

    def _expire(self, now: float):
        stale = [k for k, v in self._pending.items() if now - v['t'] > self.PENDING_TIMEOUT]
        for k in stale:
            del self._pending[k]
            self.incomplete_count += 1
        while len(self._pending) > self.MAX_PENDING:
            del self._pending[next(iter(self._pending))]
            self.incomplete_count += 1


# ── Sample helpers ─────────────────────────────────────────────────────────

def oti_camera_transform(sample: dict) -> dict:
    """Pick the camera transform: id contains 'camera', else the last in the chain."""
    transforms = sample.get('transforms') or []
    if not transforms:
        return {}
    for t in transforms:
        if 'camera' in str(t.get('id', '')).lower():
            return t
    return transforms[-1]


def oti_timecode(sample: dict):
    """Return (h, m, s, f) from timing.timecode, or None."""
    tc = (sample.get('timing') or {}).get('timecode')
    if not tc:
        return None
    try:
        return (int(tc['hours']), int(tc['minutes']), int(tc['seconds']), int(tc['frames']))
    except (KeyError, TypeError, ValueError):
        return None


def oti_device_label(sample: dict) -> str:
    """Short human-readable name for the sending device."""
    st = sample.get('static') or {}
    trk, cam = st.get('tracker') or {}, st.get('camera') or {}
    parts = []
    if trk.get('make') or trk.get('model'):
        parts.append(f"{trk.get('make', '')} {trk.get('model', '')}".strip())
    if cam.get('label'):
        parts.append(cam['label'])
    elif cam.get('model'):
        parts.append(cam['model'])
    return ' · '.join(parts) or 'OpenTrackIO'


def _s24(value: float) -> bytes:
    v = int(round(value))
    v = max(-0x800000, min(0x7FFFFF, v))
    return (v & 0xFFFFFF).to_bytes(3, 'big')


def oti_to_freed_packet(sample: dict, camera_id: int = None) -> bytes:
    """
    Convert an OpenTrackIO sample to a plain 29-byte FreeD D1 packet (no timecode).

    Units follow this app's existing FreeD conventions:
      pan/tilt/roll  degrees × 32768
      x/y/z          metres × 64000   (1/64 mm)
      zoom           focal length mm × 1000   (lens.pinholeFocalLength)
      focus          focus distance m × 1000  (lens.focusDistance)
    Byte 26 upper nibble carries a genlock phase counter that cycles while
    timing.synchronization.locked is true (constant 0 when unlocked), so
    FreeD lock detection matches the OpenTrackIO sync state.
    """
    t      = oti_camera_transform(sample)
    rot    = t.get('rotation') or {}
    pos    = t.get('translation') or {}
    lens   = sample.get('lens') or {}
    timing = sample.get('timing') or {}
    sync   = timing.get('synchronization') or {}

    pan = float(rot.get('pan', 0.0))
    if abs(pan) >= 256.0:                      # beyond 24-bit range — wrap to ±180
        pan = (pan + 180.0) % 360.0 - 180.0

    if camera_id is None:
        camera_id = sample.get('sourceNumber', 1)

    pkt = bytearray(29)
    pkt[0] = 0xD1
    pkt[1] = int(camera_id) & 0xFF
    pkt[2:5]   = _s24(pan * 32768.0)
    pkt[5:8]   = _s24(float(rot.get('tilt', 0.0)) * 32768.0)
    pkt[8:11]  = _s24(float(rot.get('roll', 0.0)) * 32768.0)
    pkt[11:14] = _s24(float(pos.get('x', 0.0)) * 64000.0)
    pkt[14:17] = _s24(float(pos.get('y', 0.0)) * 64000.0)
    pkt[17:20] = _s24(float(pos.get('z', 0.0)) * 64000.0)
    pkt[20:23] = _s24(float(lens.get('pinholeFocalLength') or 0.0) * 1000.0)
    pkt[23:26] = _s24(float(lens.get('focusDistance') or 0.0) * 1000.0)
    if sync.get('locked'):
        seq = timing.get('sequenceNumber', (sample.get('_meta') or {}).get('seq', 0))
        pkt[26] = (int(seq) & 0xF) << 4
    pkt[28] = (0xF6 - pkt[26] - pkt[27]) & 0xFF
    return bytes(pkt)
