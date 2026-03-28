#!/usr/bin/env python3
"""
FreeD Protocol Reader
Reads and parses FreeD camera tracking data from UDP socket

Version : v1.0
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

__version__   = 'v1.7'
__author__    = 'Libor Cevelik'
__copyright__ = 'Copyright (c) 2026 Libor Cevelik'

import ctypes
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QSpinBox, QPushButton, QLineEdit, QComboBox,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QColor
from datetime import datetime
os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PyQt6')
import pyqtgraph as pg
import numpy as np
from protocol import FreeDParser, FreeDReceiver, FreeDReceiverGUI
from opentrackio import OpenTrackIOSender

# Platform-aware font selection
if sys.platform == 'darwin':
    _FONT_MONO = 'Menlo'
    _FONT_SANS = 'SF Pro Text'
elif sys.platform == 'win32':
    _FONT_MONO = 'Consolas'
    _FONT_SANS = 'Segoe UI'
else:                            # Linux / other
    _FONT_MONO = 'DejaVu Sans Mono'
    _FONT_SANS = 'DejaVu Sans'

# Configure UTF-8 encoding for Windows console.
# In --noconsole (windowed) mode stdout/stderr are None; redirect to devnull
# so that any print() call anywhere in the code never raises an AttributeError
# and silently kills a background thread.
if sys.platform == 'win32' and 'pytest' not in sys.modules:
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, TypeError):
        # Windowed mode — no console attached; write to devnull instead of None
        _devnull = open(os.devnull, 'w')
        sys.stdout = _devnull
        sys.stderr = _devnull



# ── Config / SDK paths ─────────────────────────────────────────────────────
# Store config in %APPDATA%\FreeDReader so it persists regardless of where
# the EXE lives. Falls back to the script directory when running from source.
_APP_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'FreeDReader')
os.makedirs(_APP_DIR, exist_ok=True)
_CONFIG_PATH = os.path.join(_APP_DIR, 'freed_forwarder_config.json')
_BF_DLL_PATH = r'C:\Program Files\Bluefish444\Developer\driver\Release\BlueVelvetC64.dll'


# ══════════════════════════════════════════════════════════════════════════════
# BluefishLTCReader
# ══════════════════════════════════════════════════════════════════════════════

class _LtcSyncStruct(ctypes.Structure):
    _fields_ = [
        ('TimeCodeValue',   ctypes.c_uint64),
        ('TimeCodeIsValid', ctypes.c_uint32),
        ('_pad',            ctypes.c_uint8 * 20),
    ]


class BluefishLTCReader:
    """
    Reads external LTC timecode from a Bluefish444 card via ctypes.
    bfcWaitExternalLtcInputSync (blocking) is called in a daemon thread.
    .available = False if DLL missing or card not found — all other code
    gracefully falls back to system clock.
    """

    def __init__(self, dll_path: str = _BF_DLL_PATH):
        self.available   = False
        self.init_error  = ''   # populated with failure reason if init fails
        self.running     = False
        self._lock       = threading.Lock()
        self._h = self._m = self._s = self._f = 0
        self._valid      = False
        self._handle     = None
        self._dll        = None
        self._thread     = None
        self._connector  = self.CONNECTOR_INTERLOCK  # default: Interlock MMCX
        self._init_sdk(dll_path)

    # EXT LTC source connector constants (EBlueExternalLtcSource)
    CONNECTOR_BREAKOUT_HEADER = 0   # Epoch PCB header
    CONNECTOR_GENLOCK_BNC     = 1   # Reference/Genlock BNC (Epoch + Kronos)
    CONNECTOR_INTERLOCK       = 2   # Interlock MMCX (Kronos only)
    CONNECTOR_STEM_PORT       = 3   # STEM port (Kronos only)
    _EXTERNAL_LTC_SOURCE_SEL  = 120 # EXTERNAL_LTC_SOURCE_SELECTION property ID

    def _init_sdk(self, dll_path: str):
        try:
            dll = ctypes.WinDLL(dll_path)
        except Exception as e:
            self.init_error = f'DLL load failed: {e}'
            return
        try:
            dll.bfcFactory.restype  = ctypes.c_void_p
            dll.bfcFactory.argtypes = []
            dll.bfcDestroy.restype  = None
            dll.bfcDestroy.argtypes = [ctypes.c_void_p]
            dll.bfcAttach.restype   = ctypes.c_int32
            dll.bfcAttach.argtypes  = [ctypes.c_void_p, ctypes.c_int32]
            dll.bfcDetach.restype   = ctypes.c_int32
            dll.bfcDetach.argtypes  = [ctypes.c_void_p]
            dll.bfcWaitExternalLtcInputSync.restype  = ctypes.c_int32
            dll.bfcWaitExternalLtcInputSync.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_LtcSyncStruct)]
        except Exception as e:
            self.init_error = f'Function setup failed: {e}'
            return
        try:
            dll.bfcEnumerate.restype  = ctypes.c_int32
            dll.bfcEnumerate.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_int32)]
        except Exception:
            pass
        try:
            handle = dll.bfcFactory()
        except Exception as e:
            self.init_error = f'bfcFactory() raised: {e}'
            return
        if handle is None:
            self.init_error = 'bfcFactory() returned None (no card?)'
            return
        # Enumerate how many cards the driver sees
        card_count = ctypes.c_int32(0)
        try:
            dll.bfcEnumerate(handle, ctypes.byref(card_count))
        except Exception:
            pass
        n = card_count.value
        # bfcAttach uses 1-based device IDs (1 = first card, 2 = second, etc.)
        attached_idx = -1
        last_err = -1
        for idx in range(1, max(n, 1) + 1):
            try:
                last_err = dll.bfcAttach(handle, idx)
            except Exception as e:
                self.init_error = f'bfcAttach({idx}) raised: {e}'
                dll.bfcDestroy(handle)
                return
            if last_err == 0:
                attached_idx = idx
                break
        if attached_idx < 0:
            self.init_error = (
                f'bfcAttach() failed on all {max(n,1)} card(s) '
                f'(last err={last_err}). Driver sees {n} card(s).')
            dll.bfcDestroy(handle)
            return
        # Card attached successfully
        self._dll      = dll
        self._handle   = handle
        self.available = True
        # Apply LTC connector selection (failure here doesn't block availability)
        try:
            dll.bfcSetCardProperty32.restype  = ctypes.c_int32
            dll.bfcSetCardProperty32.argtypes = [
                ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32]
            dll.bfcSetCardProperty32(
                handle, self._EXTERNAL_LTC_SOURCE_SEL,
                ctypes.c_uint32(self._connector))
        except Exception as e:
            self.init_error = f'Connector select failed (card still ok): {e}'

    def set_connector(self, connector_idx: int):
        """Change the LTC input connector at runtime (0–3)."""
        self._connector = connector_idx
        if self._dll and self._handle:
            try:
                self._dll.bfcSetCardProperty32(
                    self._handle, self._EXTERNAL_LTC_SOURCE_SEL,
                    ctypes.c_uint32(connector_idx))
            except Exception:
                pass

    @staticmethod
    def _decode(value: int) -> tuple:
        return (
            ((value >> 56) & 0x3) * 10 + ((value >> 48) & 0xF),  # hours
            ((value >> 40) & 0x7) * 10 + ((value >> 32) & 0xF),  # minutes
            ((value >> 24) & 0x7) * 10 + ((value >> 16) & 0xF),  # seconds
            ((value >>  8) & 0x3) * 10 + ((value >>  0) & 0xF),  # frames
        )

    def start(self):
        if not self.available:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='BluefishLTC')
        self._thread.start()

    def stop(self):
        self.running = False
        if self._dll and self._handle:
            try:
                self._dll.bfcDetach(self._handle)
                self._dll.bfcDestroy(self._handle)
            except Exception:
                pass
            self._handle = None

    def get(self) -> tuple:
        """Returns (h, m, s, f, valid)."""
        with self._lock:
            return self._h, self._m, self._s, self._f, self._valid

    def _run(self):
        sync = _LtcSyncStruct()
        while self.running and self._handle:
            try:
                err = self._dll.bfcWaitExternalLtcInputSync(
                    self._handle, ctypes.byref(sync))
                with self._lock:
                    if err == 0 and sync.TimeCodeIsValid:
                        self._h, self._m, self._s, self._f = self._decode(sync.TimeCodeValue)
                        self._valid = True
                    else:
                        self._valid = False
            except Exception:
                with self._lock:
                    self._valid = False
                time.sleep(0.1)


# ══════════════════════════════════════════════════════════════════════════════
# FreeDForwarder
# ══════════════════════════════════════════════════════════════════════════════

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
        # Bytes 26–27: H:M:S bit-pack (backward-compat spare field)
        #   bits [15:11] = hours (5 bits), [10:5] = minutes (6 bits),
        #   [4:0] = seconds // 2 (5 bits, 2-second resolution)
        wire = ((h & 0x1F) << 11) | ((m & 0x3F) << 5) | ((s >> 1) & 0x1F)
        raw[26] = (wire >> 8) & 0xFF
        raw[27] =  wire       & 0xFF
        checksum = 0
        for b in raw[:28]:
            checksum ^= b
        raw[28] = checksum
        # Bytes 29–32: extended TC block — full H:M:S:F, one byte each
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
        except Exception:
            pass

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


class FreeDDashboard(QMainWindow):
    """Apple-dark PyQt6 dashboard for FreeD Protocol Reader"""

    BG     = '#1c1c1e'
    CARD   = '#2c2c2e'
    BORDER = '#3a3a3c'
    DIM    = '#8e8e93'
    FG     = '#f2f2f7'
    GREEN  = '#30d158'
    CYAN   = '#32ade6'
    YELLOW = '#ffd60a'
    ORANGE = '#ff9f0a'
    RED    = '#ff453a'

    def __init__(self):
        super().__init__()
        self.receiver        = None
        self.recv_thread     = None
        self._cached_ip_str  = None
        self.forwarder       = FreeDForwarder()
        self._active_port = self.forwarder.listen_port
        self.oti_sender   = OpenTrackIOSender()
        # Apply persisted OTI settings (forwarder.load_config() already ran)
        self.oti_sender.enabled      = self.forwarder.oti_enabled
        self.oti_sender.ip           = self.forwarder.oti_ip
        self.oti_sender.port         = self.forwarder.oti_port
        self.oti_sender.subject_name = self.forwarder.oti_subject
        self.oti_sender._source_id   = self.forwarder.oti_source_id
        self.ltc_reader   = BluefishLTCReader()
        # Apply saved connector choice (calls bfcSetCardProperty32 if card attached)
        self.ltc_reader.set_connector(self.forwarder.ltc_connector)
        # Resolve 'auto' source: use BlueFish if available, else system clock
        if self.forwarder.tc_source == 'auto':
            self.forwarder.tc_source = 'bluefish' if self.ltc_reader.available else 'system'
        self._build_ui()
        self._start_receiver(self._active_port)
        self.ltc_reader.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._do_update)
        self._timer.start(100)

    # ------------------------------------------------------------------
    # Stylesheet
    # ------------------------------------------------------------------

    def _stylesheet(self) -> str:
        return f"""
            QMainWindow, QWidget {{
                background-color: {self.BG};
                color: {self.FG};
            }}
            QFrame#card {{
                background-color: {self.CARD};
                border: 1px solid {self.BORDER};
                border-radius: 12px;
            }}
            QFrame#header {{
                background-color: {self.CARD};
                border-bottom: 1px solid {self.BORDER};
            }}
            QTabWidget::pane {{
                border: none;
                background-color: {self.BG};
            }}
            QTabBar::tab {{
                background-color: {self.BG};
                color: {self.DIM};
                padding: 8px 20px;
                border: none;
                font-size: 13px;
            }}
            QTabBar::tab:selected {{
                color: {self.FG};
                border-bottom: 2px solid {self.CYAN};
            }}
            QTabBar::tab:hover {{
                color: {self.FG};
            }}
            QTableWidget {{
                background-color: {self.CARD};
                border: 1px solid {self.BORDER};
                border-radius: 8px;
                gridline-color: {self.BORDER};
                color: {self.FG};
            }}
            QHeaderView::section {{
                background-color: {self.BG};
                color: {self.DIM};
                border: none;
                border-bottom: 1px solid {self.BORDER};
                padding: 6px 10px;
                font-size: 11px;
                font-weight: bold;
            }}
            QTableWidget::item {{
                padding: 4px 8px;
            }}
            QScrollBar:vertical {{
                background: {self.BG};
                width: 8px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.BORDER};
                border-radius: 4px;
            }}
        """

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(f'FreeD Dashboard {__version__}')
        self.resize(980, 620)
        self.setMinimumSize(720, 500)
        self.setStyleSheet(self._stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._build_header())
        main_layout.addWidget(self._build_tabs())

    def _build_header(self) -> QFrame:
        hdr = QFrame()
        hdr.setObjectName('header')
        hdr.setFixedHeight(46)
        layout = QHBoxLayout(hdr)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(12)

        title = QLabel('FreeD DASHBOARD')
        title.setFont(QFont(_FONT_SANS, 12, QFont.Weight.Bold))
        title.setStyleSheet(f'color: {self.FG}; background: transparent;')
        layout.addWidget(title)

        self.lbl_cam = QLabel('CAM --')
        self.lbl_cam.setFont(QFont(_FONT_SANS, 11, QFont.Weight.Bold))
        self.lbl_cam.setStyleSheet(f'color: {self.YELLOW}; background: transparent;')
        layout.addWidget(self.lbl_cam)

        layout.addStretch()

        ver = QLabel(f'{__version__}  ·  {__author__}')
        ver.setFont(QFont(_FONT_SANS, 9))
        ver.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        layout.addWidget(ver)

        self.lbl_status = QLabel('● WAITING')
        self.lbl_status.setFont(QFont(_FONT_SANS, 10, QFont.Weight.Bold))
        self.lbl_status.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        layout.addWidget(self.lbl_status)

        return hdr

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        dash = QWidget()
        self._build_dashboard(dash)
        tabs.addTab(dash, '  Dashboard  ')

        pmap = QWidget()
        self._build_packet_map(pmap)
        tabs.addTab(pmap, '  Packet Map  ')

        jitter = QWidget()
        self._build_jitter_tab(jitter)
        tabs.addTab(jitter, '  Jitter  ')

        settings = QWidget()
        self._build_settings_tab(settings)
        tabs.addTab(settings, '  Settings  ')

        return tabs

    def _card(self, title: str):
        """Create a rounded card. Returns (outer_widget, QFormLayout)."""
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName('card')
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 10, 14, 12)
        inner.setSpacing(6)

        if title:
            hdr_lbl = QLabel(title)
            hdr_lbl.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
            hdr_lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            inner.addWidget(hdr_lbl)

        form_widget = QWidget()
        form_widget.setStyleSheet('background: transparent;')
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 2, 0, 0)
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        inner.addWidget(form_widget)

        outer_layout.addWidget(frame)
        return outer, form

    def _key(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont(_FONT_SANS, 9))
        lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def _val(self, color: str, mono: bool = True, size: int = 12) -> QLabel:
        lbl = QLabel('---')
        family = _FONT_MONO if mono else _FONT_SANS
        lbl.setFont(QFont(family, size, QFont.Weight.Bold))
        lbl.setStyleSheet(f'color: {color}; background: transparent;')
        return lbl

    def _build_dashboard(self, parent: QWidget):
        grid = QGridLayout(parent)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)

        # ROTATION
        rot_outer, rot_form = self._card('ROTATION')
        self.lbl_pan  = self._val(self.GREEN)
        self.lbl_tilt = self._val(self.GREEN)
        self.lbl_roll = self._val(self.GREEN)
        rot_form.addRow(self._key('Pan'),  self.lbl_pan)
        rot_form.addRow(self._key('Tilt'), self.lbl_tilt)
        rot_form.addRow(self._key('Roll'), self.lbl_roll)
        grid.addWidget(rot_outer, 0, 0)

        # POSITION
        pos_outer, pos_form = self._card('POSITION')
        self.lbl_x = self._val(self.CYAN)
        self.lbl_y = self._val(self.CYAN)
        self.lbl_z = self._val(self.CYAN)
        pos_form.addRow(self._key('X'), self.lbl_x)
        pos_form.addRow(self._key('Y'), self.lbl_y)
        pos_form.addRow(self._key('Z'), self.lbl_z)
        grid.addWidget(pos_outer, 0, 1)

        # LENS
        lens_outer, lens_form = self._card('LENS')
        self.lbl_zoom  = self._val(self.YELLOW)
        self.lbl_focus = self._val(self.YELLOW)
        lens_form.addRow(self._key('Zoom'),  self.lbl_zoom)
        lens_form.addRow(self._key('Focus'), self.lbl_focus)
        grid.addWidget(lens_outer, 1, 0)

        # GENLOCK
        gl_outer = QWidget()
        gl_vbox = QVBoxLayout(gl_outer)
        gl_vbox.setContentsMargins(0, 0, 0, 0)
        gl_frame = QFrame()
        gl_frame.setObjectName('card')
        gl_inner = QVBoxLayout(gl_frame)
        gl_inner.setContentsMargins(14, 10, 14, 12)
        gl_inner.setSpacing(6)

        gl_title = QLabel('GENLOCK')
        gl_title.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        gl_title.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        gl_inner.addWidget(gl_title)

        self.lbl_gl_status = QLabel('● WAITING')
        self.lbl_gl_status.setFont(QFont(_FONT_SANS, 15, QFont.Weight.Bold))
        self.lbl_gl_status.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        self.lbl_gl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gl_inner.addWidget(self.lbl_gl_status)

        gl_fw = QWidget()
        gl_fw.setStyleSheet('background: transparent;')
        gl_form = QFormLayout(gl_fw)
        gl_form.setContentsMargins(0, 2, 0, 0)
        gl_form.setSpacing(5)
        gl_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.lbl_gl_phase = self._val(self.CYAN,   size=11)
        self.lbl_gl_ref   = self._val(self.FG,     size=10, mono=True)
        self.lbl_gl_freq  = self._val(self.ORANGE, size=14)
        self.lbl_gl_raw   = self._val(self.DIM,    size=10)
        self.lbl_gl_ref.setFont(QFont(_FONT_MONO, 10))
        self.lbl_gl_raw.setFont(QFont(_FONT_MONO, 10))
        gl_form.addRow(self._key('Phase'), self.lbl_gl_phase)
        gl_form.addRow(self._key('Ref'),   self.lbl_gl_ref)
        gl_form.addRow(self._key('Freq'),  self.lbl_gl_freq)
        gl_form.addRow(self._key('Bytes'), self.lbl_gl_raw)
        gl_inner.addWidget(gl_fw)
        gl_vbox.addWidget(gl_frame)
        grid.addWidget(gl_outer, 1, 1)

        # STATUS
        st_outer = QWidget()
        st_vbox = QVBoxLayout(st_outer)
        st_vbox.setContentsMargins(0, 0, 0, 0)
        st_frame = QFrame()
        st_frame.setObjectName('card')
        st_inner = QVBoxLayout(st_frame)
        st_inner.setContentsMargins(14, 10, 14, 12)
        st_inner.setSpacing(6)

        st_title = QLabel('STATUS')
        st_title.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        st_title.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        st_inner.addWidget(st_title)

        self.lbl_tc = QLabel('--:--:--:--')
        self.lbl_tc.setFont(QFont(_FONT_MONO, 22, QFont.Weight.Bold))
        self.lbl_tc.setStyleSheet(f'color: {self.ORANGE}; background: transparent;')
        self.lbl_tc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        st_inner.addWidget(self.lbl_tc)

        st_fw = QWidget()
        st_fw.setStyleSheet('background: transparent;')
        st_form = QFormLayout(st_fw)
        st_form.setContentsMargins(0, 2, 0, 0)
        st_form.setSpacing(5)
        st_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.lbl_packets  = self._val(self.FG,  size=11)
        self.lbl_source   = self._val(self.DIM, size=9, mono=False)
        self.lbl_port     = self._val(self.DIM, size=9, mono=False)
        self.lbl_interval = self._val(self.CYAN, size=11)
        self.lbl_source.setFont(QFont(_FONT_SANS, 9))
        self.lbl_port.setFont(QFont(_FONT_SANS, 9))
        st_form.addRow(self._key('Packets'),  self.lbl_packets)
        st_form.addRow(self._key('Source'),   self.lbl_source)
        st_form.addRow(self._key('Port'),     self.lbl_port)
        st_form.addRow(self._key('Interval'), self.lbl_interval)
        self.lbl_port.setText(str(self._active_port))
        st_inner.addWidget(st_fw)
        st_vbox.addWidget(st_frame)
        grid.addWidget(st_outer, 2, 0)

        # RAW PACKET
        raw_outer, raw_form = self._card('RAW PACKET')
        self.lbl_proto   = self._val(self.CYAN, size=11)
        self.lbl_rawsize = self._val(self.FG,   size=11)
        self.lbl_hex1    = self._val('#aaaaaa',  size=9)
        self.lbl_hex2    = self._val('#aaaaaa',  size=9)
        self.lbl_hex1.setFont(QFont(_FONT_MONO, 9))
        self.lbl_hex2.setFont(QFont(_FONT_MONO, 9))
        raw_form.addRow(self._key('Proto'), self.lbl_proto)
        raw_form.addRow(self._key('Size'),  self.lbl_rawsize)
        raw_form.addRow(self._key('Hex'),   self.lbl_hex1)
        raw_form.addRow(self._key(''),      self.lbl_hex2)
        grid.addWidget(raw_outer, 2, 1)

    def _build_packet_map(self, parent: QWidget):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(10, 10, 10, 10)

        tbl = QTableWidget(12, 4)
        tbl.setHorizontalHeaderLabels(['Hex Bytes', 'Field', 'Raw Value', 'Decoded'])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        tbl.setColumnWidth(0, 110)
        tbl.setColumnWidth(1, 100)
        tbl.setColumnWidth(2, 120)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.setAlternatingRowColors(False)

        rows = [
            (self.DIM,    '--',       'Msg Type'),
            (self.DIM,    '--',       'Cam ID'),
            (self.GREEN,  '-- -- --', 'Pan'),
            (self.GREEN,  '-- -- --', 'Tilt'),
            (self.GREEN,  '-- -- --', 'Roll'),
            (self.CYAN,   '-- -- --', 'X'),
            (self.CYAN,   '-- -- --', 'Y'),
            (self.CYAN,   '-- -- --', 'Z'),
            (self.YELLOW, '-- -- --', 'Zoom'),
            (self.YELLOW, '-- -- --', 'Focus'),
            (self.ORANGE, '-- --',    'Spare/GL'),
            (self.DIM,    '--',       'Checksum'),
        ]

        self._pm_font = QFont(_FONT_MONO, 10)
        for i, (color, hex_ph, field) in enumerate(rows):
            qc = QColor(color)
            for col, text in enumerate([hex_ph, field, '---', '---']):
                item = QTableWidgetItem(text)
                item.setForeground(qc)
                item.setFont(self._pm_font)
                tbl.setItem(i, col, item)
            tbl.setRowHeight(i, 26)

        layout.addWidget(tbl)
        self.packet_table = tbl
        self._pm_colors = [row[0] for row in rows]

    def _build_jitter_tab(self, parent: QWidget):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # ── Health banner ─────────────────────────────────────────────
        banner_frame = QFrame()
        banner_frame.setObjectName('card')
        banner_layout = QHBoxLayout(banner_frame)
        banner_layout.setContentsMargins(16, 10, 16, 10)
        banner_layout.setSpacing(16)

        self._jitter_health_dot = QLabel('●')
        self._jitter_health_dot.setFont(QFont(_FONT_SANS, 18, QFont.Weight.Bold))
        self._jitter_health_dot.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        banner_layout.addWidget(self._jitter_health_dot)

        banner_text = QWidget()
        banner_text.setStyleSheet('background: transparent;')
        banner_text_v = QVBoxLayout(banner_text)
        banner_text_v.setContentsMargins(0, 0, 0, 0)
        banner_text_v.setSpacing(1)

        self._jitter_health_lbl = QLabel('WAITING FOR DATA')
        self._jitter_health_lbl.setFont(QFont(_FONT_SANS, 13, QFont.Weight.Bold))
        self._jitter_health_lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        banner_text_v.addWidget(self._jitter_health_lbl)

        self._jitter_health_sub = QLabel('Measuring packet timing jitter — how consistently packets arrive')
        self._jitter_health_sub.setFont(QFont(_FONT_SANS, 9))
        self._jitter_health_sub.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        banner_text_v.addWidget(self._jitter_health_sub)

        banner_layout.addWidget(banner_text, stretch=1)

        # Thresholds legend
        thresh_w = QWidget()
        thresh_w.setStyleSheet('background: transparent;')
        thresh_l = QVBoxLayout(thresh_w)
        thresh_l.setContentsMargins(0, 0, 0, 0)
        thresh_l.setSpacing(2)
        for dot, label in [('●', f'Ideal  < 1ms'), ('●', 'Accept  1–3ms'), ('●', 'Problem > 5ms')]:
            color = [self.GREEN, self.YELLOW, self.RED][['●', '●', '●'].index(dot) if False else [0,1,2].pop(0)]
            row = QLabel(f'<span style="color:{color}">●</span>  {label}')
            row.setFont(QFont(_FONT_SANS, 9))
            row.setStyleSheet('color: #8e8e93; background: transparent;')
            thresh_l.addWidget(row)
        banner_layout.addWidget(thresh_w)

        layout.addWidget(banner_frame)

        # ── Stats row ────────────────────────────────────────────────
        stats_row = QWidget()
        stats_row.setStyleSheet('background: transparent;')
        stats_layout = QHBoxLayout(stats_row)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(10)

        stat_defs = [
            ('MEAN',       self.CYAN),
            ('STD DEV',    self.ORANGE),
            ('MIN',        self.GREEN),
            ('MAX',        self.RED),
            ('PEAK  ±',    self.YELLOW),
            ('RFC JITTER', self.FG),
        ]
        self._jitter_stat_labels = {}
        self._jitter_stat_frames = {}
        for title, color in stat_defs:
            frame = QFrame()
            frame.setObjectName('card')
            vbox = QVBoxLayout(frame)
            vbox.setContentsMargins(10, 8, 10, 10)
            vbox.setSpacing(1)
            lbl_t = QLabel(title)
            lbl_t.setFont(QFont(_FONT_SANS, 8, QFont.Weight.Bold))
            lbl_t.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            lbl_t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_v = QLabel('---')
            lbl_v.setFont(QFont(_FONT_MONO, 13, QFont.Weight.Bold))
            lbl_v.setStyleSheet(f'color: {color}; background: transparent;')
            lbl_v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vbox.addWidget(lbl_t)
            vbox.addWidget(lbl_v)
            stats_layout.addWidget(frame)
            self._jitter_stat_labels[title] = lbl_v
            self._jitter_stat_frames[title] = frame

        layout.addWidget(stats_row)

        # ── Line graph: interval over time ────────────────────────────
        line_card = QFrame()
        line_card.setObjectName('card')
        line_vbox = QVBoxLayout(line_card)
        line_vbox.setContentsMargins(10, 8, 10, 10)
        line_vbox.setSpacing(4)

        line_hdr = QLabel('PACKET INTERVAL OVER TIME  (last 200 packets)   — dashed lines: 1ms ideal / 3ms acceptable / 5ms problematic')
        line_hdr.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        line_hdr.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        line_vbox.addWidget(line_hdr)

        self._jitter_plot = pg.PlotWidget()
        self._jitter_plot.setBackground(self.CARD)
        for axis in ('left', 'bottom'):
            self._jitter_plot.getAxis(axis).setPen(pg.mkPen(self.BORDER))
            self._jitter_plot.getAxis(axis).setTextPen(pg.mkPen(self.DIM))
        self._jitter_plot.showGrid(x=False, y=True, alpha=0.15)
        self._jitter_plot.setLabel('left',   'ms', color=self.DIM)
        self._jitter_plot.setLabel('bottom', 'packet #', color=self.DIM)
        self._jitter_curve = self._jitter_plot.plot(
            pen=pg.mkPen(color=self.CYAN, width=1.5))
        self._jitter_mean_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(color=self.GREEN, style=Qt.PenStyle.DashLine, width=1))
        self._jitter_plot.addItem(self._jitter_mean_line)
        # Threshold reference lines
        for ms, color in [(1.0, self.GREEN), (3.0, self.YELLOW), (5.0, self.RED)]:
            line = pg.InfiniteLine(
                pos=ms, angle=0,
                pen=pg.mkPen(color=color, style=Qt.PenStyle.DotLine, width=1))
            self._jitter_plot.addItem(line)
        line_vbox.addWidget(self._jitter_plot)
        layout.addWidget(line_card, stretch=3)

        # ── Histogram: interval distribution ─────────────────────────
        hist_card = QFrame()
        hist_card.setObjectName('card')
        hist_vbox = QVBoxLayout(hist_card)
        hist_vbox.setContentsMargins(10, 8, 10, 10)
        hist_vbox.setSpacing(4)

        hist_hdr = QLabel('INTERVAL DISTRIBUTION  (last 500 packets)')
        hist_hdr.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        hist_hdr.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        hist_vbox.addWidget(hist_hdr)

        self._hist_plot = pg.PlotWidget()
        self._hist_plot.setBackground(self.CARD)
        for axis in ('left', 'bottom'):
            self._hist_plot.getAxis(axis).setPen(pg.mkPen(self.BORDER))
            self._hist_plot.getAxis(axis).setTextPen(pg.mkPen(self.DIM))
        self._hist_plot.showGrid(x=False, y=True, alpha=0.15)
        self._hist_plot.setLabel('left',   'count',  color=self.DIM)
        self._hist_plot.setLabel('bottom', 'ms',     color=self.DIM)
        self._hist_bars = pg.BarGraphItem(
            x=[], height=[], width=1,
            brush=pg.mkBrush(self.ORANGE),
            pen=pg.mkPen(self.BORDER))
        self._hist_plot.addItem(self._hist_bars)
        hist_vbox.addWidget(self._hist_plot)
        layout.addWidget(hist_card, stretch=2)

    def _build_settings_tab(self, parent: QWidget):
        outer = QVBoxLayout(parent)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Sub-tab bar inside Settings
        sub_tabs = QTabWidget()
        sub_tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {self.BG};
            }}
            QTabBar::tab {{
                background-color: {self.BG};
                color: {self.DIM};
                padding: 6px 16px;
                border: none;
                font-size: 12px;
                font-family: {_FONT_SANS};
            }}
            QTabBar::tab:selected {{
                color: {self.FG};
                border-bottom: 2px solid {self.CYAN};
            }}
            QTabBar::tab:hover {{
                color: {self.FG};
            }}
        """)
        outer.addWidget(sub_tabs)

        # ── Network sub-tab ───────────────────────────────────────────
        net_page = QWidget()
        net_page.setStyleSheet(f'background-color: {self.BG};')
        net_layout = QVBoxLayout(net_page)
        net_layout.setContentsMargins(10, 10, 10, 10)
        net_layout.setSpacing(10)
        net_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        net_frame = QFrame()
        net_frame.setObjectName('card')
        net_inner = QVBoxLayout(net_frame)
        net_inner.setContentsMargins(16, 12, 16, 14)
        net_inner.setSpacing(10)

        row = QWidget()
        row.setStyleSheet('background: transparent;')
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)

        port_lbl = QLabel('UDP Port')
        port_lbl.setFont(QFont(_FONT_SANS, 11))
        port_lbl.setStyleSheet(f'color: {self.FG}; background: transparent;')
        row_layout.addWidget(port_lbl)

        self._settings_port_spin = QSpinBox()
        self._settings_port_spin.setRange(1024, 65535)
        self._settings_port_spin.setValue(self._active_port)
        self._settings_port_spin.setFixedWidth(100)
        self._settings_port_spin.setStyleSheet(f"""
            QSpinBox {{
                background-color: {self.BG}; color: {self.FG};
                border: 1px solid {self.BORDER}; border-radius: 6px;
                padding: 4px 8px; font-family: {_FONT_MONO}; font-size: 13px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 18px; background-color: {self.BORDER}; border-radius: 3px;
            }}
        """)
        row_layout.addWidget(self._settings_port_spin)

        apply_btn = QPushButton('Apply')
        apply_btn.setFixedWidth(80)
        apply_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.CYAN}; color: #000000;
                border: none; border-radius: 6px; padding: 5px 14px;
                font-family: {_FONT_SANS}; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #5ac8fa; }}
            QPushButton:pressed {{ background-color: #0a84ff; }}
        """)
        apply_btn.clicked.connect(self._on_apply_port)
        row_layout.addWidget(apply_btn)
        row_layout.addStretch()
        net_inner.addWidget(row)

        self._settings_status = QLabel(f'● Listening on port {self._active_port}')
        self._settings_status.setFont(QFont(_FONT_SANS, 10))
        self._settings_status.setStyleSheet(f'color: {self.GREEN}; background: transparent;')
        net_inner.addWidget(self._settings_status)

        net_layout.addWidget(net_frame)
        sub_tabs.addTab(net_page, 'Network')

        # ── Output Destinations sub-tab ───────────────────────────────
        dest_page = QWidget()
        dest_page.setStyleSheet(f'background-color: {self.BG};')
        dest_layout = QVBoxLayout(dest_page)
        dest_layout.setContentsMargins(10, 10, 10, 10)
        dest_layout.setSpacing(10)
        dest_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        dest_frame = QFrame()
        dest_frame.setObjectName('card')
        dest_outer = QVBoxLayout(dest_frame)
        dest_outer.setContentsMargins(16, 12, 16, 14)
        dest_outer.setSpacing(8)

        # Column header
        hdr = QWidget()
        hdr.setStyleSheet('background: transparent;')
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(0, 0, 0, 0)
        hdr_l.setSpacing(8)
        for txt, w in [('IP / Broadcast', 160), ('Port', 85), ('Enable', 50), ('', 30)]:
            lbl = QLabel(txt)
            lbl.setFixedWidth(w)
            lbl.setFont(QFont(_FONT_SANS, 9))
            lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            hdr_l.addWidget(lbl)
        hdr_l.addStretch()
        dest_outer.addWidget(hdr)

        rows_container = QWidget()
        rows_container.setStyleSheet('background: transparent;')
        self._dest_rows_layout = QVBoxLayout(rows_container)
        self._dest_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._dest_rows_layout.setSpacing(4)
        dest_outer.addWidget(rows_container)

        self._dest_rows = []
        for d in self.forwarder.destinations:
            self._add_dest_row(d)

        add_row_w = QWidget()
        add_row_w.setStyleSheet('background: transparent;')
        add_row_l = QHBoxLayout(add_row_w)
        add_row_l.setContentsMargins(0, 4, 0, 0)
        add_row_l.setSpacing(0)
        add_btn = QPushButton('+ Add destination')
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent; color: {self.CYAN};
                border: 1px solid {self.CYAN}; border-radius: 6px;
                padding: 4px 12px; font-family: {_FONT_SANS}; font-size: 12px;
            }}
            QPushButton:hover {{ background-color: {self.CARD}; }}
        """)
        add_btn.clicked.connect(self._on_add_dest)
        add_row_l.addWidget(add_btn)
        add_row_l.addStretch()
        dest_outer.addWidget(add_row_w)

        self._fwd_count_lbl = QLabel('Forwarded: 0 pkts')
        self._fwd_count_lbl.setFont(QFont(_FONT_SANS, 10))
        self._fwd_count_lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        dest_outer.addWidget(self._fwd_count_lbl)

        # ── OpenTrackIO section ───────────────────────────────────────
        oti_frame = QFrame()
        oti_frame.setStyleSheet(f'''
            QFrame {{ background-color: {self.CARD}; border-radius: 10px;
                      border: 1px solid {self.BORDER}; }}
        ''')
        oti_outer = QVBoxLayout(oti_frame)
        oti_outer.setContentsMargins(14, 10, 14, 12)
        oti_outer.setSpacing(8)

        oti_hdr = QLabel('OpenTrackIO Output')
        oti_hdr.setFont(QFont(_FONT_SANS, 11, QFont.Weight.Bold))
        oti_hdr.setStyleSheet(f'color: {self.FG}; background: transparent;')
        oti_outer.addWidget(oti_hdr)

        oti_sub = QLabel('UDP · JSON · OpenTrackIO v1.0.1 · SMPTE RIS-OSVP')
        oti_sub.setFont(QFont(_FONT_SANS, 9))
        oti_sub.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        oti_outer.addWidget(oti_sub)

        _field_style = f'''QLineEdit {{
            background: {self.BG}; color: {self.FG}; border: 1px solid {self.BORDER};
            border-radius: 6px; padding: 4px 8px;
            font-family: {_FONT_MONO}; font-size: 12px;
        }}'''

        # Enable + IP + Port row
        oti_row_w = QWidget(); oti_row_w.setStyleSheet('background: transparent;')
        oti_row_l = QHBoxLayout(oti_row_w)
        oti_row_l.setContentsMargins(0, 0, 0, 0); oti_row_l.setSpacing(8)

        self._oti_enable_btn = QPushButton('OFF')
        self._oti_enable_btn.setCheckable(True)
        self._oti_enable_btn.setFixedWidth(50)
        self._oti_enable_btn.setChecked(self.oti_sender.enabled)
        self._oti_enable_btn.setStyleSheet(self._dest_toggle_style(self.oti_sender.enabled))
        self._oti_enable_btn.toggled.connect(self._on_oti_toggle)
        oti_row_l.addWidget(self._oti_enable_btn)

        self._oti_ip = QLineEdit(self.oti_sender.ip)
        self._oti_ip.setFixedWidth(160)
        self._oti_ip.setStyleSheet(_field_style)
        self._oti_ip.setPlaceholderText('127.0.0.1')
        self._oti_ip.editingFinished.connect(self._on_oti_ip_changed)
        oti_row_l.addWidget(self._oti_ip)

        oti_colon = QLabel(':')
        oti_colon.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        oti_row_l.addWidget(oti_colon)

        self._oti_port = QLineEdit(str(self.oti_sender.port))
        self._oti_port.setFixedWidth(70)
        self._oti_port.setStyleSheet(_field_style)
        self._oti_port.setPlaceholderText('55555')
        self._oti_port.editingFinished.connect(self._on_oti_port_changed)
        oti_row_l.addWidget(self._oti_port)
        oti_row_l.addStretch()
        oti_outer.addWidget(oti_row_w)


        dest_layout.addWidget(dest_frame)
        dest_layout.addWidget(oti_frame)
        sub_tabs.addTab(dest_page, 'Output')

        # ── Timecode sub-tab ──────────────────────────────────────────
        tc_page = QWidget()
        tc_page.setStyleSheet(f'background-color: {self.BG};')
        tc_layout = QVBoxLayout(tc_page)
        tc_layout.setContentsMargins(10, 10, 10, 10)
        tc_layout.setSpacing(10)
        tc_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        tc_frame = QFrame()
        tc_frame.setObjectName('card')
        tc_outer = QVBoxLayout(tc_frame)
        tc_outer.setContentsMargins(16, 12, 16, 14)
        tc_outer.setSpacing(12)

        _combo_style = f"""
            QComboBox {{
                background-color: {self.BG}; color: {self.FG};
                border: 1px solid {self.BORDER}; border-radius: 6px;
                padding: 4px 8px; font-family: {_FONT_SANS}; font-size: 12px;
            }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background-color: {self.CARD}; color: {self.FG};
                selection-background-color: {self.CYAN}; selection-color: #000000;
            }}
        """

        def _tc_row(label_text):
            w = QWidget(); w.setStyleSheet('background: transparent;')
            hl = QHBoxLayout(w); hl.setContentsMargins(0,0,0,0); hl.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(140)
            lbl.setFont(QFont(_FONT_SANS, 11))
            lbl.setStyleSheet(f'color: {self.FG}; background: transparent;')
            hl.addWidget(lbl)
            return w, hl

        # Source
        src_w, src_l = _tc_row('Source')
        self._tc_src_combo = QComboBox()
        self._tc_src_combo.setFixedWidth(220)
        self._tc_src_combo.setStyleSheet(_combo_style)
        self._tc_src_combo.addItem('System Clock', 'system')
        self._tc_src_combo.addItem(
            'BlueFish LTC (ext)' if self.ltc_reader.available
            else 'BlueFish LTC (not detected)', 'bluefish')
        for i in range(self._tc_src_combo.count()):
            if self._tc_src_combo.itemData(i) == self.forwarder.tc_source:
                self._tc_src_combo.setCurrentIndex(i); break
        self._tc_src_combo.currentIndexChanged.connect(self._on_tc_source_changed)
        src_l.addWidget(self._tc_src_combo)
        src_l.addStretch()
        tc_outer.addWidget(src_w)

        # LTC Connector (only visible when BlueFish source selected)
        conn_w, conn_l = _tc_row('LTC Connector')
        self._ltc_conn_combo = QComboBox()
        self._ltc_conn_combo.setFixedWidth(200)
        self._ltc_conn_combo.setStyleSheet(_combo_style)
        _conn_options = [
            (0, 'Breakout Header (PCB)'),
            (1, 'Genlock / Ref BNC'),
            (2, 'Interlock MMCX'),
            (3, 'STEM Port'),
        ]
        for idx, label in _conn_options:
            self._ltc_conn_combo.addItem(label, idx)
        # Pre-select saved connector
        for i in range(self._ltc_conn_combo.count()):
            if self._ltc_conn_combo.itemData(i) == self.forwarder.ltc_connector:
                self._ltc_conn_combo.setCurrentIndex(i); break
        self._ltc_conn_combo.currentIndexChanged.connect(self._on_ltc_connector_changed)
        conn_l.addWidget(self._ltc_conn_combo)
        conn_l.addStretch()
        tc_outer.addWidget(conn_w)

        # FPS
        fps_w, fps_l = _tc_row('TC FPS')
        self._tc_fps_combo = QComboBox()
        self._tc_fps_combo.setFixedWidth(110)
        self._tc_fps_combo.setStyleSheet(_combo_style)
        _fps_labels = {23.976: '23.976', 24.0: '24', 25.0: '25',
                       29.97: '29.97', 30.0: '30', 48.0: '48', 50.0: '50', 60.0: '60'}
        for v in FreeDForwarder.FPS_OPTIONS:
            self._tc_fps_combo.addItem(_fps_labels.get(v, str(v)), v)
        for i in range(self._tc_fps_combo.count()):
            if abs(self._tc_fps_combo.itemData(i) - self.forwarder.tc_fps) < 0.01:
                self._tc_fps_combo.setCurrentIndex(i); break
        self._tc_fps_combo.currentIndexChanged.connect(self._on_tc_fps_changed)
        fps_l.addWidget(self._tc_fps_combo)
        fps_l.addStretch()
        tc_outer.addWidget(fps_w)

        # Preview
        prev_w, prev_l = _tc_row('Preview')
        self._tc_preview_lbl = QLabel('--:--:--:--')
        self._tc_preview_lbl.setFont(QFont(_FONT_MONO, 13))
        self._tc_preview_lbl.setStyleSheet(f'color: {self.CYAN}; background: transparent;')
        prev_l.addWidget(self._tc_preview_lbl)
        prev_l.addStretch()
        tc_outer.addWidget(prev_w)

        tc_layout.addWidget(tc_frame)
        sub_tabs.addTab(tc_page, 'Timecode')

    def _on_apply_port(self):
        port = self._settings_port_spin.value()
        if port == self._active_port:
            return
        self._settings_status.setText(f'● Restarting on port {port}…')
        self._settings_status.setStyleSheet(f'color: {self.YELLOW}; background: transparent;')
        QApplication.processEvents()
        self._restart_receiver(port)
        self.forwarder.listen_port = port
        self.forwarder.save_config()
        self._settings_status.setText(f'● Listening on port {port}')
        self._settings_status.setStyleSheet(f'color: {self.GREEN}; background: transparent;')

    # ------------------------------------------------------------------
    # Destination row helpers
    # ------------------------------------------------------------------

    def _dest_toggle_style(self, on: bool) -> str:
        if on:
            return f"""QPushButton {{
                background-color: {self.GREEN}; color: #000000;
                border: none; border-radius: 6px; padding: 4px 6px;
                font-family: {_FONT_SANS}; font-size: 11px; font-weight: bold;
            }}"""
        return f"""QPushButton {{
            background-color: {self.BG}; color: {self.DIM};
            border: 1px solid {self.BORDER}; border-radius: 6px; padding: 4px 6px;
            font-family: {_FONT_SANS}; font-size: 11px;
        }}"""

    def _add_dest_row(self, d: dict = None):
        if d is None:
            d = {'ip': '', 'port': 45000, 'enabled': False}
        permanent = d.get('permanent', False)

        row_w = QWidget()
        row_w.setStyleSheet('background: transparent;')
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(8)

        ip_edit = QLineEdit()
        ip_edit.setPlaceholderText('IP or 255.255.255.255')
        ip_edit.setText(d.get('ip', ''))
        ip_edit.setFixedWidth(160)
        ip_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {self.BG}; color: {self.FG};
                border: 1px solid {self.BORDER}; border-radius: 6px;
                padding: 4px 8px; font-family: {_FONT_MONO}; font-size: 12px;
            }}
            QLineEdit:focus {{ border-color: {self.CYAN}; }}
        """)
        row_l.addWidget(ip_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(d.get('port', 45000))
        port_spin.setFixedWidth(85)
        port_spin.setStyleSheet(f"""
            QSpinBox {{
                background-color: {self.BG}; color: {self.FG};
                border: 1px solid {self.BORDER}; border-radius: 6px;
                padding: 4px 6px; font-family: {_FONT_MONO}; font-size: 12px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 16px; background-color: {self.BORDER}; border-radius: 3px;
            }}
        """)
        row_l.addWidget(port_spin)

        enabled = d.get('enabled', False)
        en_btn = QPushButton('ON' if enabled else 'OFF')
        en_btn.setCheckable(True)
        en_btn.setChecked(enabled)
        en_btn.setFixedWidth(50)
        en_btn.setStyleSheet(self._dest_toggle_style(enabled))
        en_btn.clicked.connect(lambda checked, b=en_btn: self._on_dest_enable(b, checked))
        row_l.addWidget(en_btn)

        if permanent:
            spacer = QLabel('')
            spacer.setFixedWidth(28)
            row_l.addWidget(spacer)
        else:
            rm_btn = QPushButton('✕')
            rm_btn.setFixedWidth(28)
            rm_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent; color: {self.RED};
                    border: 1px solid {self.RED}; border-radius: 6px;
                    font-size: 11px; padding: 2px;
                }}
                QPushButton:hover {{ background-color: {self.RED}; color: #ffffff; }}
            """)
            row_l.addWidget(rm_btn)

        row_l.addStretch()

        row_info = {'widget': row_w, 'ip': ip_edit, 'port': port_spin,
                    'enable': en_btn, 'permanent': permanent}
        self._dest_rows.append(row_info)
        self._dest_rows_layout.addWidget(row_w)

        ip_edit.textChanged.connect(self._sync_destinations)
        port_spin.valueChanged.connect(self._sync_destinations)
        if not permanent:
            rm_btn.clicked.connect(lambda _, ri=row_info: self._remove_dest_row(ri))

    def _on_dest_enable(self, btn: QPushButton, checked: bool):
        btn.setText('ON' if checked else 'OFF')
        btn.setStyleSheet(self._dest_toggle_style(checked))
        self._sync_destinations()

    def _remove_dest_row(self, row_info: dict):
        self._dest_rows = [r for r in self._dest_rows if r is not row_info]
        row_info['widget'].setParent(None)
        row_info['widget'].deleteLater()
        self._sync_destinations()

    def _on_add_dest(self):
        self._add_dest_row()
        self._sync_destinations()

    def _sync_destinations(self):
        dests = []
        for r in self._dest_rows:
            d = {'ip': r['ip'].text().strip(),
                 'port': r['port'].value(),
                 'enabled': r['enable'].isChecked()}
            if r.get('permanent'):
                d['permanent'] = True
            dests.append(d)
        self.forwarder.destinations = dests
        self.forwarder.save_config()

    # ------------------------------------------------------------------
    # Timecode injection helpers
    # ------------------------------------------------------------------

    def _on_tc_source_changed(self, idx: int):
        self.forwarder.tc_source = self._tc_src_combo.itemData(idx) or 'system'
        self.forwarder.save_config()

    def _on_tc_fps_changed(self, idx: int):
        self.forwarder.tc_fps = self._tc_fps_combo.itemData(idx) or 25.0
        self.forwarder.save_config()

    def _on_ltc_connector_changed(self, idx: int):
        connector = self._ltc_conn_combo.itemData(idx)
        if connector is None:
            return
        self.forwarder.ltc_connector = connector
        self.ltc_reader.set_connector(connector)
        self.forwarder.save_config()

    def _on_oti_toggle(self, checked: bool):
        self.oti_sender.enabled = checked
        self._oti_enable_btn.setText('ON' if checked else 'OFF')
        self._oti_enable_btn.setStyleSheet(self._dest_toggle_style(checked))
        self.forwarder.oti_enabled = checked
        self.forwarder.save_config()

    def _on_oti_ip_changed(self):
        ip = self._oti_ip.text().strip()
        if ip:
            self.oti_sender.ip = ip
            self.forwarder.oti_ip = ip
            self.forwarder.save_config()

    def _on_oti_port_changed(self):
        try:
            p = int(self._oti_port.text())
            if 1 <= p <= 65535:
                self.oti_sender.port = p
                self.forwarder.oti_port = p
                self.forwarder.save_config()
        except ValueError:
            pass

    def _on_parsed_packet(self, data: dict):
        """Enrich FreeD data with calibrated lens values then forward to OTI."""
        try:
            r = self.receiver
            if 'zoom' in data:
                data['focal_length_mm']  = r.interpolate_zoom(data['zoom'])
            if 'focus' in data:
                data['focus_distance_m'] = r.interpolate_focus(data['focus'])
        except Exception as e:
            print(f'[OTI] lens enrich error: {e}', flush=True)
        self.oti_sender.send(data, self.ltc_reader, self.forwarder.tc_fps)

    # ------------------------------------------------------------------
    # Receiver (background thread)
    # ------------------------------------------------------------------

    def _start_receiver(self, port: int = 45000):
        self.receiver = FreeDReceiverGUI(
            host='0.0.0.0',
            port=port,
            ignore_checksum=True,
            timecode_fps=24.0,
            convert_units=True,
            clear_screen=False,
            debug=False,
        )
        try:
            self.receiver.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.receiver.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.receiver.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            self.receiver.socket.settimeout(1.0)
            self.receiver.socket.bind(('0.0.0.0', port))
            self.receiver.running = True
        except Exception as e:
            self.lbl_status.setText(f'● ERROR: {e}')
            self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            return

        self.receiver.on_packet = lambda raw: self.forwarder.forward(raw, self.ltc_reader)
        self.receiver.on_packet_parsed = self._on_parsed_packet
        self.recv_thread = threading.Thread(
            target=self.receiver.receive_loop,
            daemon=True,
            name='FreeDReceiveLoop',
        )
        self.recv_thread.start()

    def _restart_receiver(self, port: int):
        # Stop existing receiver
        if self.receiver:
            self.receiver.running = False
            try:
                self.receiver.socket.close()
            except Exception:
                pass
        if self.recv_thread and self.recv_thread.is_alive():
            self.recv_thread.join(timeout=2.0)

        self._active_port = port
        self.lbl_port.setText(str(port))
        self._start_receiver(port)

    # ------------------------------------------------------------------
    # Update loop (10 Hz via QTimer)
    # ------------------------------------------------------------------

    def _do_update(self):
        try:
            self._update()
        except Exception as e:
            try:
                self.lbl_status.setText(f'● UI ERR: {str(e)[:40]}')
                self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            except Exception:
                pass
        self._update_fwd_ui()

    def _update_fwd_ui(self):
        try:
            self._fwd_count_lbl.setText(
                f'Forwarded: {self.forwarder.packets_forwarded:,} pkts')
            self._tc_preview_lbl.setText(
                self.forwarder.current_tc_str(self.ltc_reader))
        except Exception:
            pass

    def _update(self):
        if self.receiver is None:
            return

        if self.recv_thread is not None and not self.recv_thread.is_alive():
            err = self.receiver._last_error or 'unknown error'
            self.lbl_status.setText(f'● RX DEAD: {err[:40]}')
            self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            return

        data = self.receiver.latest_data
        addr = self.receiver.latest_addr

        if data is None:
            if self._cached_ip_str is None:
                try:
                    all_ips = sorted({
                        info[4][0]
                        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                        if not info[4][0].startswith('127.')
                    })
                    self._cached_ip_str = '  /  '.join(all_ips) if all_ips else '0.0.0.0'
                except Exception:
                    self._cached_ip_str = '0.0.0.0'
            self.lbl_status.setText(f'● LISTENING :{self._active_port}  [{self._cached_ip_str}]')
            self.lbl_status.setStyleSheet(f'color: {self.CYAN}; background: transparent;')
            return

        r = self.receiver
        now = time.monotonic()
        is_stale = (r._last_packet_time is not None) and ((now - r._last_packet_time) > 2.0)

        # Rotation
        pan_deg  = data['pan']  * r.rotation_scale
        tilt_deg = data['tilt'] * r.rotation_scale
        roll_deg = data['roll'] * r.rotation_scale
        rot_color = self.DIM if is_stale else self.GREEN
        self.lbl_pan.setText(f'{pan_deg:+8.2f}°  [{data["pan"]}]')
        self.lbl_pan.setStyleSheet(f'color: {rot_color}; background: transparent;')
        self.lbl_tilt.setText(f'{tilt_deg:+8.2f}°  [{data["tilt"]}]')
        self.lbl_tilt.setStyleSheet(f'color: {rot_color}; background: transparent;')
        self.lbl_roll.setText(f'{roll_deg:+8.2f}°  [{data["roll"]}]')
        self.lbl_roll.setStyleSheet(f'color: {rot_color}; background: transparent;')

        # Position
        x_m = data['position']['x'] * r.position_scale / 1000.0
        y_m = data['position']['y'] * r.position_scale / 1000.0
        z_m = data['position']['z'] * r.position_scale / 1000.0
        pos_color = self.DIM if is_stale else self.CYAN
        self.lbl_x.setText(f'{x_m:+7.3f} m  [{data["position"]["x"]}]')
        self.lbl_x.setStyleSheet(f'color: {pos_color}; background: transparent;')
        self.lbl_y.setText(f'{y_m:+7.3f} m  [{data["position"]["y"]}]')
        self.lbl_y.setStyleSheet(f'color: {pos_color}; background: transparent;')
        self.lbl_z.setText(f'{z_m:+7.3f} m  [{data["position"]["z"]}]')
        self.lbl_z.setStyleSheet(f'color: {pos_color}; background: transparent;')

        # Lens
        focal_length   = data['zoom']  / 1000.0 if data['zoom'] != 0 else None
        focus_distance = abs(data['focus'] / 1000.0) if data['focus'] not in (0, 65535) else None
        total_inches   = focus_distance * 39.3701 if focus_distance is not None else 0.0
        feet           = int(total_inches // 12)
        frac_in        = total_inches % 12
        lens_color = self.DIM if is_stale else self.YELLOW
        if focal_length is None:
            self.lbl_zoom.setText(f'---  [{data["zoom"]}]')
            self.lbl_zoom.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        else:
            self.lbl_zoom.setText(f'{focal_length:.1f} mm  [{data["zoom"]}]')
            self.lbl_zoom.setStyleSheet(f'color: {lens_color}; background: transparent;')
        if focus_distance is None:
            self.lbl_focus.setText(f'---  [{data["focus"]}]')
            self.lbl_focus.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        else:
            self.lbl_focus.setText(f'{focus_distance:.2f}m  {feet}ft {frac_in:.1f}in  [{data["focus"]}]')
            self.lbl_focus.setStyleSheet(f'color: {lens_color}; background: transparent;')

        # Timecode — prefer extended block (bytes 29–32) for full H:M:S:F
        ext = data.get('ext_tc')
        if ext:
            tc = f'{ext[0]:02d}:{ext[1]:02d}:{ext[2]:02d}:{ext[3]:02d}'
        else:
            tc = r.parse_timecode(data['spare'], 1.0)
        self.lbl_tc.setText(tc or '--:--:--:--')

        # Stats
        self.lbl_packets.setText(f"{r.parser.packet_count:,}")
        self.lbl_cam.setText(f"CAM {data['camera_id']}")
        if addr:
            self.lbl_source.setText(f'{addr[0]}:{addr[1]}')
        if is_stale:
            self.lbl_status.setText('● TIMEOUT')
            self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
        else:
            self.lbl_status.setText('● LIVE')
            self.lbl_status.setStyleSheet(f'color: {self.GREEN}; background: transparent;')

        if r.packet_interval_ms is not None:
            fps = r.packet_fps
            self.lbl_interval.setText(f'{r.packet_interval_ms:.1f} ms  ({fps:.1f} fps)')

        # Raw packet
        msg_type   = data['message_type']
        proto_name = f'D{msg_type & 0x0F}  (0x{msg_type:02X})'
        self.lbl_proto.setText(proto_name)
        self.lbl_rawsize.setText(f"{data['packet_size']} bytes")
        raw = data['raw_bytes']
        mid   = len(raw) // 2
        line1 = ' '.join(f'{b:02X}' for b in raw[:mid])
        line2 = ' '.join(f'{b:02X}' for b in raw[mid:])
        self.lbl_hex1.setText(line1)
        self.lbl_hex2.setText(line2)

        # Genlock
        rb        = data['raw_bytes']
        gl_byte26 = rb[26]
        gl_byte27 = rb[27]
        gl_phase  = (gl_byte26 >> 4) & 0xF
        if is_stale:
            self.lbl_gl_status.setText('● NO SIGNAL')
            self.lbl_gl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            self.lbl_gl_phase.setText('---')
            self.lbl_gl_freq.setText('--- Hz')
            self.lbl_gl_raw.setText('-- --')
        else:
            is_locked = len(set(r._gl_phase_history)) > 1
            self.lbl_gl_status.setText('● LOCKED' if is_locked else '● UNLOCKED')
            self.lbl_gl_status.setStyleSheet(
                f'color: {self.GREEN if is_locked else self.RED}; background: transparent;')
            self.lbl_gl_phase.setText(f'{gl_phase:X}h  ({gl_phase}/16)')
            if r.packet_fps is not None:
                self.lbl_gl_freq.setText(f'{r.packet_fps:.2f} Hz')
            self.lbl_gl_raw.setText(f'0x{gl_byte26:02X} 0x{gl_byte27:02X}  [{gl_byte26:08b}]')
        self.lbl_gl_ref.setText(f'0x{gl_byte27:02X} (vendor-defined)')

        # Packet Map
        if hasattr(self, 'packet_table'):
            rb          = data['raw_bytes']
            gl_phase_pm = (rb[26] >> 4) & 0xF
            lock_str    = 'LOCKED' if len(set(r._gl_phase_history)) > 1 else 'UNLOCKED'
            map_rows = [
                (f'{rb[0]:02X}',
                 'Msg Type', str(rb[0]),
                 f'D{rb[0] & 0x0F} Protocol'),
                (f'{rb[1]:02X}',
                 'Cam ID', str(rb[1]),
                 f'Camera {rb[1]}'),
                (' '.join(f'{b:02X}' for b in rb[2:5]),
                 'Pan', str(data['pan']),
                 f'{pan_deg:+.2f}°'),
                (' '.join(f'{b:02X}' for b in rb[5:8]),
                 'Tilt', str(data['tilt']),
                 f'{tilt_deg:+.2f}°'),
                (' '.join(f'{b:02X}' for b in rb[8:11]),
                 'Roll', str(data['roll']),
                 f'{roll_deg:+.2f}°'),
                (' '.join(f'{b:02X}' for b in rb[11:14]),
                 'X', str(data['position']['x']),
                 f'{x_m:+.3f} m'),
                (' '.join(f'{b:02X}' for b in rb[14:17]),
                 'Y', str(data['position']['y']),
                 f'{y_m:+.3f} m'),
                (' '.join(f'{b:02X}' for b in rb[17:20]),
                 'Z', str(data['position']['z']),
                 f'{z_m:+.3f} m'),
                (' '.join(f'{b:02X}' for b in rb[20:23]),
                 'Zoom', str(data['zoom']),
                 f'{focal_length:.1f} mm' if focal_length is not None else '---'),
                (' '.join(f'{b:02X}' for b in rb[23:26]),
                 'Focus', str(data['focus']),
                 f'{focus_distance:.2f}m  {feet}ft {frac_in:.1f}in' if focus_distance is not None else '---'),
                (f'{rb[26]:02X} {rb[27]:02X}',
                 'Spare/GL', f'0x{data["spare"]:04X}',
                 f'{lock_str}  ph={gl_phase_pm:X}h  ref=0x{rb[27]:02X}'),
                (f'{rb[28]:02X}',
                 'Checksum', f'0x{rb[28]:02X}',
                 'OK' if data['checksum_valid'] else 'MISMATCH'),
            ]
            mono = self._pm_font
            for i, (hx, field, raw_val, decoded) in enumerate(map_rows):
                qc = QColor(self._pm_colors[i])
                for col, text in enumerate([hx, field, raw_val, decoded]):
                    item = self.packet_table.item(i, col)
                    if item is None:
                        item = QTableWidgetItem(text)
                        item.setFont(mono)
                        self.packet_table.setItem(i, col, item)
                    else:
                        item.setText(text)
                    item.setForeground(qc)

        # Jitter tab
        if hasattr(self, '_jitter_stat_labels'):
            self._update_jitter_tab()

    def _update_jitter_tab(self):
        r       = self.receiver
        history = list(r._jitter_history)
        n       = len(history)

        if n < 2:
            for lbl in self._jitter_stat_labels.values():
                lbl.setText('---')
            return

        arr  = np.array(history, dtype=np.float64)
        mean = float(np.mean(arr))
        std  = float(np.std(arr))
        mn   = float(np.min(arr))
        mx   = float(np.max(arr))
        peak = max(abs(mx - mean), abs(mn - mean))
        rfc  = r._rfc_jitter

        self._jitter_stat_labels['MEAN'].setText(f'{mean:.2f} ms')
        self._jitter_stat_labels['STD DEV'].setText(f'{std:.2f} ms')
        self._jitter_stat_labels['MIN'].setText(f'{mn:.1f} ms')
        self._jitter_stat_labels['MAX'].setText(f'{mx:.1f} ms')
        self._jitter_stat_labels['PEAK  ±'].setText(f'±{peak:.2f} ms')
        self._jitter_stat_labels['RFC JITTER'].setText(f'{rfc:.2f} ms')

        # Health assessment based on RFC jitter (packet timing tolerance for LED volumes)
        if rfc < 1.0:
            health_color = self.GREEN
            health_title = 'IDEAL'
            health_sub   = f'RFC jitter {rfc:.2f} ms — safe for LED volume, no visible judder expected'
        elif rfc < 3.0:
            health_color = self.YELLOW
            health_title = 'ACCEPTABLE'
            health_sub   = f'RFC jitter {rfc:.2f} ms — minor timing variation, monitor on fast pans'
        elif rfc < 5.0:
            health_color = self.ORANGE
            health_title = 'MARGINAL'
            health_sub   = f'RFC jitter {rfc:.2f} ms — approaching problematic, check network/switch'
        else:
            health_color = self.RED
            health_title = 'PROBLEMATIC'
            health_sub   = f'RFC jitter {rfc:.2f} ms — visible judder likely on LED wall, fix network'

        self._jitter_health_dot.setStyleSheet(f'color: {health_color}; background: transparent;')
        self._jitter_health_lbl.setText(health_title)
        self._jitter_health_lbl.setStyleSheet(f'color: {health_color}; background: transparent;')
        self._jitter_health_sub.setText(health_sub)

        # Color RFC JITTER stat card dynamically
        self._jitter_stat_labels['RFC JITTER'].setStyleSheet(
            f'color: {health_color}; background: transparent;')

        # Line graph — last 200 samples
        recent = arr[-200:] if n >= 200 else arr
        self._jitter_curve.setData(recent.tolist())
        self._jitter_mean_line.setValue(mean)

        # Histogram — all 500 samples, 30 bins
        counts, edges = np.histogram(arr, bins=min(30, n))
        centers = ((edges[:-1] + edges[1:]) / 2).tolist()
        width   = float(edges[1] - edges[0]) * 0.85
        self._hist_bars.setOpts(x=centers, height=counts.tolist(), width=width)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._timer.stop()
        self.ltc_reader.stop()
        self.forwarder.save_config()
        self.forwarder.close()
        self.oti_sender.close()
        if self.receiver:
            self.receiver.running = False
            if self.receiver.socket:
                try:
                    self.receiver.socket.close()
                except Exception:
                    pass
        event.accept()



def main_gui():
    """GUI entry point — launches Apple-dark PyQt6 dashboard"""
    app = QApplication(sys.argv)
    app.setApplicationName('FreeD Dashboard')
    window = FreeDDashboard()
    window.show()
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())

def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='FreeD Protocol Reader - Debug and analyze FreeD camera tracking data')
    parser.add_argument('--host', default='0.0.0.0', help='IP address to listen on (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=45000, help='UDP port to listen on (default: 45000)')
    parser.add_argument('--debug', '-d', action='store_true', help='Enable debug mode (show raw packet data)')
    parser.add_argument('--step', '-s', action='store_true', help='Step-by-step mode (wait for Enter between packets)')
    parser.add_argument('--delay', type=float, default=0.0, help='Delay in seconds between packets (e.g., 0.5 for slower viewing)')
    parser.add_argument('--ignore-checksum', '-i', action='store_true', default=True, help='Parse and display data even if checksum fails')
    parser.add_argument('--timecode', '-t', type=float, metavar='FPS', default=24.0, help='Parse spare bytes as timecode with specified FPS (e.g., 25, 30, 29.97, 24)')
    parser.add_argument('--convert', '-c', action='store_true', default=True, help='Convert to real-world units (degrees, meters, focal length)')
    parser.add_argument('--clear', '-r', action='store_true', default=True, help='Clear screen and refresh in place (live dashboard mode)')
    parser.add_argument('--position-scale', type=float, default=1.0/64.0, help='Position scale factor (default: 1/64 for mm)')
    parser.add_argument('--rotation-scale', type=float, default=1.0/32768.0, help='Rotation scale factor (default: 1/32768 for degrees)')
    # Zoom and Focus calibration are now hardcoded in the FreeDReceiver class
    # Edit the zoom_calibration and focus_calibration lists in the code to adjust calibration points

    args = parser.parse_args()

    print("=" * 80)
    print("FreeD Protocol Reader")
    if args.debug:
        print("DEBUG MODE: Showing raw packet data and validation details")
    if args.step:
        print("STEP-BY-STEP MODE: Press Enter to view each packet")
    if args.delay > 0:
        print(f"DELAY MODE: {args.delay} second delay between packets")
    if args.ignore_checksum:
        print("IGNORE CHECKSUM: Parsing data even with checksum errors")
    if args.timecode:
        print(f"TIMECODE MODE: Parsing spare bytes as timecode @ {args.timecode} fps")
    if args.convert:
        print("UNIT CONVERSION: Showing real-world units (degrees, meters, focal length)")
    if args.clear:
        print("REFRESH MODE: Screen will clear and update in place")
    print("=" * 80)

    receiver = FreeDReceiver(
        host=args.host,
        port=args.port,
        debug=args.debug,
        step_by_step=args.step,
        delay=args.delay,
        ignore_checksum=args.ignore_checksum,
        timecode_fps=args.timecode,
        convert_units=args.convert,
        clear_screen=args.clear
    )

    # Apply custom scale factors if provided
    if args.position_scale:
        receiver.position_scale = args.position_scale
    if args.rotation_scale:
        receiver.rotation_scale = args.rotation_scale

    receiver.start()


if __name__ == '__main__':
    if '--cli' in sys.argv or '--no-gui' in sys.argv:
        sys.argv = [a for a in sys.argv if a not in ('--cli', '--no-gui')]
        main()
    else:
        main_gui()
