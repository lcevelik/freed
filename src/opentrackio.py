"""
OpenTrackIO sender — converts FreeD data to OpenTrackIO v1.0.1 JSON over UDP.
Spec: SMPTE RIS-OSVP / opentrackio.org
"""

import json
import socket
import struct
import threading
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
