"""
FreeDForwarder — forwards FreeD packets (with optional TC injection) to multiple
UDP destinations. Config is persisted to %APPDATA%\\FreeDReader\\freed_forwarder_config.json.
"""
import json
import os
import socket
import sys
import threading
import uuid
from datetime import datetime

_APP_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'FreeDReader')
os.makedirs(_APP_DIR, exist_ok=True)
_CONFIG_PATH = os.path.join(_APP_DIR, 'freed_forwarder_config.json')


class FreeDForwarder:
    """
    Forwards FreeD packets (with optional TC injection) to multiple UDP
    destinations.  Config is persisted to a JSON file next to the script.
    """

    FPS_OPTIONS = [23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 60.0]

    def __init__(self, config_path: str = _CONFIG_PATH):
        self._config_path      = config_path
        self.destinations      = []
        self.tc_inject         = False
        self.tc_source         = 'system'   # 'system' | 'bluefish'
        self.tc_fps            = 25.0
        self.ltc_connector     = 2          # EXT_LTC_SRC_INTERLOCK default
        self.packets_forwarded = 0
        self._lock             = threading.Lock()
        self._sock             = None
        self._open_socket()
        self.load_config()

    def _open_socket(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock = s
        except Exception:
            self._sock = None

    # ── forwarding ─────────────────────────────────────────────────────────

    def forward(self, raw: bytes, ltc_reader=None):
        """Called from the receive thread on every valid packet."""
        if self._sock is None:
            return
        buf = bytearray(raw)
        if self.tc_inject and len(buf) == 29:
            buf = self._inject_tc(buf, ltc_reader)
        payload = bytes(buf)
        with self._lock:
            dests = list(self.destinations)
        for d in dests:
            if not d.get('enabled', False):
                continue
            ip   = d.get('ip', '').strip()
            port = d.get('port', 45000)
            if not ip:
                continue
            try:
                self._sock.sendto(payload, (ip, port))
                self.packets_forwarded += 1
            except Exception:
                pass

    def _inject_tc(self, raw: bytearray, ltc_reader) -> bytearray:
        fps_int = max(1, round(self.tc_fps))
        if self.tc_source == 'bluefish' and ltc_reader and ltc_reader.available:
            h, m, s, f, valid = ltc_reader.get()
            if not valid:
                h, m, s, f = self._system_tc(fps_int)
        else:
            h, m, s, f = self._system_tc(fps_int)
        # Bytes 26-27: H:M:S bit-pack (backward-compat spare field)
        #   bits [15:11] = hours (5 bits), [10:5] = minutes (6 bits),
        #   [4:0] = seconds // 2 (5 bits, 2-second resolution)
        wire = ((h & 0x1F) << 11) | ((m & 0x3F) << 5) | ((s >> 1) & 0x1F)
        raw[26] = (wire >> 8) & 0xFF
        raw[27] =  wire       & 0xFF
        raw[28] = (0xF6 - raw[26] - raw[27]) & 0xFF
        # Bytes 29-32: extended TC block — full H:M:S:F, one byte each
        raw += bytearray([h & 0xFF, m & 0xFF, s & 0xFF, f & 0xFF])
        return raw

    @staticmethod
    def _system_tc(fps_int: int) -> tuple:
        now = datetime.now()
        f   = int((now.microsecond / 1_000_000) * fps_int)
        return now.hour, now.minute, now.second, f

    def current_tc_str(self, ltc_reader=None) -> str:
        fps_int = max(1, round(self.tc_fps))
        if self.tc_source == 'bluefish' and ltc_reader and ltc_reader.available:
            h, m, s, f, valid = ltc_reader.get()
            if not valid:
                h, m, s, f = self._system_tc(fps_int)
        else:
            h, m, s, f = self._system_tc(fps_int)
        return f'{h:02d}:{m:02d}:{s:02d}:{f:02d}'

    # ── persistence ────────────────────────────────────────────────────────

    def save_config(self):
        try:
            with self._lock:
                data = {
                    'destinations':    list(self.destinations),
                    'tc_inject':       self.tc_inject,
                    'tc_source':       self.tc_source,
                    'tc_fps':          self.tc_fps,
                    'ltc_connector':   self.ltc_connector,
                    'oti_enabled':     self.oti_enabled,
                    'oti_ip':          self.oti_ip,
                    'oti_port':        self.oti_port,
                    'oti_subject':     self.oti_subject,
                    'oti_source_id':   self.oti_source_id,
                    'listen_port':     self.listen_port,
                }
            with open(self._config_path, 'w') as fh:
                json.dump(data, fh, indent=2)
        except Exception as e:
            print(f'[FreeDForwarder] save_config failed: {e}', file=sys.stderr, flush=True)

    _PERMANENT = {'ip': '127.0.0.1', 'port': 40000, 'enabled': False, 'permanent': True}

    def load_config(self):
        try:
            with open(self._config_path) as fh:
                data = json.load(fh)
            # Strip any saved permanent entry — we always re-prepend the canonical one
            user_dests = [d for d in data.get('destinations', [])
                          if not d.get('permanent', False)]
            with self._lock:
                self.destinations  = [dict(self._PERMANENT)] + user_dests
                self.tc_inject     = bool(data.get('tc_inject', True))
                self.tc_source     = data.get('tc_source', 'system')
                self.tc_fps        = float(data.get('tc_fps', 25.0))
                self.ltc_connector = int(data.get('ltc_connector', 1))
                self.oti_enabled   = bool(data.get('oti_enabled', False))
                self.oti_ip        = data.get('oti_ip', '127.0.0.1')
                self.oti_port      = int(data.get('oti_port', 55555))
                self.oti_subject   = data.get('oti_subject', 'Camera')
                self.oti_source_id = data.get('oti_source_id') or str(uuid.uuid4())
                self.listen_port   = int(data.get('listen_port', 45000))
        except Exception:
            self.destinations  = [
                dict(self._PERMANENT),
                {'ip': '', 'port': 45000, 'enabled': False},
                {'ip': '', 'port': 45000, 'enabled': False},
            ]
            self.tc_inject     = True
            self.tc_source     = 'auto'   # resolved in FreeDDashboard.__init__
            self.tc_fps        = 25.0
            self.ltc_connector = 2
            self.oti_enabled   = False
            self.oti_ip        = '127.0.0.1'
            self.oti_port      = 55555
            self.oti_subject   = 'Camera'
            self.oti_source_id = str(uuid.uuid4())
            self.listen_port   = 45000

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
