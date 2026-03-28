#!/usr/bin/env python3
"""
OpenTrackIO Simulator
Sends OpenTrackIO v1.0.1 JSON packets over UDP for testing without real hardware.
Covers the full schema: transforms, lens, distortion, camera metadata, timing.

Version : v1.0
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

import json
import socket
import struct
import sys
import uuid
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout,
    QSlider, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QGroupBox, QSizePolicy, QTabWidget, QLineEdit, QComboBox,
    QScrollArea,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont

# ── Fonts ──────────────────────────────────────────────────────────────────
if sys.platform == 'darwin':
    _FONT_MONO = 'Menlo'
    _FONT_SANS = 'SF Pro Text'
elif sys.platform == 'win32':
    _FONT_MONO = 'Consolas'
    _FONT_SANS = 'Segoe UI'
else:
    _FONT_MONO = 'DejaVu Sans Mono'
    _FONT_SANS = 'DejaVu Sans'

if sys.platform == 'win32' and 'pytest' not in sys.modules:
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except AttributeError:
        import os
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')


# ── OpenTrackIO packet builder ─────────────────────────────────────────────

def _fletcher16(data: bytes) -> int:
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) & 0xFF
        s2 = (s2 + s1) & 0xFF
    return (s2 << 8) | s1


def build_opentrackio_packet(payload: bytes, seq: int) -> bytes:
    """Wrap a JSON payload in a 16-byte OpenTrackIO UDP header + Fletcher-16."""
    n = len(payload)
    hdr = bytearray(14)
    hdr[0:4] = b'OTrk'
    hdr[4]   = 0x00                        # reserved
    hdr[5]   = 0x01                        # JSON encoding
    struct.pack_into('>H', hdr, 6, seq)    # sequence
    struct.pack_into('>I', hdr, 8, 0)      # segment offset = 0
    hdr[12]  = 0x80 | ((n >> 8) & 0x7F)   # last-seg flag + len high
    hdr[13]  = n & 0xFF                    # len low
    ck = _fletcher16(bytes(hdr) + payload)
    return bytes(hdr) + struct.pack('>H', ck) + payload


# ── Simulator GUI ──────────────────────────────────────────────────────────

class OpenTrackIOSimulator(QMainWindow):

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
    PURPLE = '#bf5af2'

    def __init__(self):
        super().__init__()
        self._sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._seq    = 0
        self._source_id = str(uuid.uuid4())
        self._sending = False

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._send_packet)

    # ── Stylesheet ─────────────────────────────────────────────────────────

    def _stylesheet(self) -> str:
        return f"""
        QMainWindow, QWidget {{
            background-color: {self.BG};
            color: {self.FG};
            font-family: '{_FONT_SANS}';
            font-size: 13px;
        }}
        QTabWidget::pane {{
            border: 1px solid {self.BORDER};
            border-radius: 8px;
            background: {self.CARD};
        }}
        QTabBar::tab {{
            background: {self.BG};
            color: {self.DIM};
            border: 1px solid {self.BORDER};
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 5px 14px;
            margin-right: 2px;
            font-size: 12px;
            font-weight: 600;
        }}
        QTabBar::tab:selected {{
            background: {self.CARD};
            color: {self.FG};
        }}
        QTabBar::tab:hover {{
            color: {self.FG};
        }}
        QFrame#card {{
            background-color: {self.CARD};
            border-radius: 12px;
            border: 1px solid {self.BORDER};
        }}
        QGroupBox {{
            background-color: {self.BG};
            border-radius: 8px;
            border: 1px solid {self.BORDER};
            margin-top: 18px;
            padding: 8px;
            font-size: 12px;
            color: {self.DIM};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
            top: 4px;
            color: {self.DIM};
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        QScrollArea {{ border: none; background: transparent; }}
        QSlider::groove:horizontal {{
            height: 4px;
            background: {self.BORDER};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {self.FG};
            width: 16px; height: 16px;
            margin: -6px 0;
            border-radius: 8px;
        }}
        QSlider::sub-page:horizontal {{
            background: {self.CYAN};
            border-radius: 2px;
        }}
        QDoubleSpinBox, QSpinBox {{
            background-color: {self.BG};
            color: {self.FG};
            border: 1px solid {self.BORDER};
            border-radius: 6px;
            padding: 2px 30px 2px 8px;
            font-family: '{_FONT_MONO}';
            font-size: 12px;
            min-height: 26px;
        }}
        QDoubleSpinBox::up-button, QSpinBox::up-button {{
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 26px; background: #48484a;
            border-left: 1px solid {self.BORDER};
            border-top-right-radius: 6px;
            border-bottom: 1px solid {self.BORDER};
        }}
        QDoubleSpinBox::down-button, QSpinBox::down-button {{
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 26px; background: #48484a;
            border-left: 1px solid {self.BORDER};
            border-bottom-right-radius: 6px;
        }}
        QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
        QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
            background: #636366;
        }}
        QDoubleSpinBox::up-button:pressed, QSpinBox::up-button:pressed,
        QDoubleSpinBox::down-button:pressed, QSpinBox::down-button:pressed {{
            background: {self.CYAN};
        }}
        QLineEdit {{
            background-color: {self.BG};
            color: {self.FG};
            border: 1px solid {self.BORDER};
            border-radius: 6px;
            padding: 4px 8px;
            font-family: '{_FONT_MONO}';
            font-size: 12px;
            min-height: 26px;
        }}
        QComboBox {{
            background-color: {self.BG};
            color: {self.FG};
            border: 1px solid {self.BORDER};
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 12px;
            min-height: 26px;
        }}
        QComboBox::drop-down {{ border: none; width: 20px; }}
        QComboBox QAbstractItemView {{
            background: {self.CARD};
            color: {self.FG};
            border: 1px solid {self.BORDER};
            selection-background-color: {self.CYAN};
            selection-color: #000;
        }}
        QCheckBox {{
            spacing: 6px;
            color: {self.FG};
        }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border-radius: 4px;
            border: 1px solid {self.BORDER};
            background: {self.BG};
        }}
        QCheckBox::indicator:checked {{
            background: {self.GREEN};
            border-color: {self.GREEN};
        }}
        QPushButton {{
            background-color: {self.CYAN};
            color: #000000;
            border: none;
            border-radius: 7px;
            padding: 5px 18px;
            font-weight: 600;
            font-size: 13px;
        }}
        QPushButton:hover {{ background-color: #5ac8fa; }}
        QPushButton#stop {{
            background-color: {self.RED};
            color: #ffffff;
        }}
        QPushButton#stop:hover {{ background-color: #ff6961; }}
        QPushButton:disabled {{
            background-color: {self.BORDER};
            color: {self.DIM};
        }}
        QLabel#dim {{
            color: {self.DIM};
            font-size: 11px;
        }}
        QLabel#value {{
            color: {self.CYAN};
            font-family: '{_FONT_MONO}';
            font-size: 12px;
        }}
        QLabel#section {{
            color: {self.FG};
            font-size: 14px;
            font-weight: 700;
        }}
        QLabel#status {{
            font-family: '{_FONT_MONO}';
            font-size: 12px;
        }}
        """

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle('OpenTrackIO Simulator v1.0')
        self.resize(860, 720)
        self.setMinimumSize(780, 600)
        self.setStyleSheet(self._stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        self._tabs.addTab(self._make_scrollable(self._build_transform_tab()),  'Transform')
        self._tabs.addTab(self._make_scrollable(self._build_lens_tab()),       'Lens')
        self._tabs.addTab(self._make_scrollable(self._build_distortion_tab()), 'Distortion')
        self._tabs.addTab(self._make_scrollable(self._build_camera_tab()),     'Camera')
        self._tabs.addTab(self._make_scrollable(self._build_timing_tab()),     'Timing')

        root.addWidget(self._build_control_bar())

    def _make_scrollable(self, widget: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(widget)
        return sa

    # ── Helpers ────────────────────────────────────────────────────────────

    def _group(self, title: str) -> tuple:
        gb = QGroupBox(title)
        lay = QVBoxLayout(gb)
        lay.setSpacing(4)
        return gb, lay

    def _form_row(self, label: str, widget: QWidget,
                  color: str = None, unit: str = '') -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lbl = QLabel(label)
        lbl.setObjectName('dim')
        lbl.setFixedWidth(150)
        lay.addWidget(lbl)
        lay.addWidget(widget)
        if unit:
            u = QLabel(unit)
            u.setObjectName('dim')
            u.setFixedWidth(40)
            lay.addWidget(u)
        lay.addStretch()
        return row

    def _slider_row(self, label: str, min_v: float, max_v: float,
                    step: float, default: float, unit: str,
                    color: str) -> tuple:
        """Returns (row_widget, spinbox). Slider + spinbox synced."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        lbl = QLabel(label)
        lbl.setObjectName('dim')
        lbl.setFixedWidth(46)
        lay.addWidget(lbl)

        scale = int(round(1 / step)) if step < 1 else 1
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(int(round(min_v * scale)), int(round(max_v * scale)))
        sl.setValue(int(round(default * scale)))
        lay.addWidget(sl)

        decimals = max(0, len(str(step).rstrip('0').split('.')[-1])) if '.' in str(step) else 0
        spin = QDoubleSpinBox()
        spin.setRange(min_v, max_v)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.setValue(default)
        spin.setSuffix(f' {unit}' if unit else '')
        spin.setFixedWidth(120)
        if color:
            spin.setStyleSheet(f'QDoubleSpinBox {{ color: {color}; }}')
        lay.addWidget(spin)

        def sl_to_spin(v): spin.blockSignals(True); spin.setValue(v / scale); spin.blockSignals(False)
        def spin_to_sl(v): sl.blockSignals(True); sl.setValue(int(round(v * scale))); sl.blockSignals(False)
        sl.valueChanged.connect(sl_to_spin)
        spin.valueChanged.connect(spin_to_sl)

        return row, spin

    def _spinbox(self, min_v, max_v, default, step=1.0, decimals=2,
                 suffix='', width=120, color=None) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(min_v, max_v)
        s.setSingleStep(step)
        s.setDecimals(decimals)
        s.setValue(default)
        s.setSuffix(f' {suffix}' if suffix else '')
        s.setFixedWidth(width)
        if color:
            s.setStyleSheet(f'QDoubleSpinBox {{ color: {color}; }}')
        return s

    def _intbox(self, min_v, max_v, default, suffix='', width=120) -> QSpinBox:
        s = QSpinBox()
        s.setRange(min_v, max_v)
        s.setValue(default)
        s.setSuffix(f' {suffix}' if suffix else '')
        s.setFixedWidth(width)
        return s

    # ── Tab: Transform ─────────────────────────────────────────────────────

    def _build_transform_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # Subject name
        gb_id, gb_lay = self._group('Subject')
        row = QWidget(); rl = QHBoxLayout(row); rl.setContentsMargins(0,0,0,0); rl.setSpacing(8)
        lbl = QLabel('Subject name'); lbl.setObjectName('dim'); lbl.setFixedWidth(150)
        self._edit_subject = QLineEdit('Camera')
        self._edit_subject.setFixedWidth(200)
        rl.addWidget(lbl); rl.addWidget(self._edit_subject); rl.addStretch()
        gb_lay.addWidget(row)
        lay.addWidget(gb_id)

        # Rotation
        gb_rot, gb_rot_lay = self._group('Rotation')
        rp, self._spin_pan  = self._slider_row('Pan',  -180.0, 180.0, 0.01, 0.0, '°', self.GREEN)
        rt, self._spin_tilt = self._slider_row('Tilt',  -90.0,  90.0, 0.01, 0.0, '°', self.GREEN)
        rr, self._spin_roll = self._slider_row('Roll', -180.0, 180.0, 0.01, 0.0, '°', self.GREEN)
        gb_rot_lay.addWidget(rp); gb_rot_lay.addWidget(rt); gb_rot_lay.addWidget(rr)
        lay.addWidget(gb_rot)

        # Translation
        gb_pos, gb_pos_lay = self._group('Translation (metres)')
        rx, self._spin_x = self._slider_row('X', -50.0, 50.0, 0.001, 0.0, 'm', self.CYAN)
        ry, self._spin_y = self._slider_row('Y', -50.0, 50.0, 0.001, 0.0, 'm', self.CYAN)
        rz, self._spin_z = self._slider_row('Z', -50.0, 50.0, 0.001, 0.0, 'm', self.CYAN)
        gb_pos_lay.addWidget(rx); gb_pos_lay.addWidget(ry); gb_pos_lay.addWidget(rz)
        lay.addWidget(gb_pos)

        lay.addStretch()
        return w

    # ── Tab: Lens ──────────────────────────────────────────────────────────

    def _build_lens_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # Focal length
        gb_fl, gb_fl_lay = self._group('Focal Length')
        rf, self._spin_focal_length = self._slider_row('Focal length', 1.0, 600.0, 0.1, 35.0, 'mm', self.YELLOW)
        self._chk_focal_length = QCheckBox('Include in packet')
        self._chk_focal_length.setChecked(True)
        gb_fl_lay.addWidget(self._chk_focal_length)
        gb_fl_lay.addWidget(rf)
        lay.addWidget(gb_fl)

        # Focus
        gb_fd, gb_fd_lay = self._group('Focus Distance')
        rfd, self._spin_focus_dist = self._slider_row('Distance', 0.1, 200.0, 0.01, 2.0, 'm', self.YELLOW)
        self._chk_focus_dist = QCheckBox('Include in packet')
        self._chk_focus_dist.setChecked(True)
        gb_fd_lay.addWidget(self._chk_focus_dist)
        gb_fd_lay.addWidget(rfd)
        lay.addWidget(gb_fd)

        # Encoders
        gb_enc, gb_enc_lay = self._group('Lens Encoders (0 – 1 normalised)')
        re_f, self._spin_enc_focus = self._slider_row('Focus', 0.0, 1.0, 0.001, 0.5, '', self.ORANGE)
        re_z, self._spin_enc_zoom  = self._slider_row('Zoom',  0.0, 1.0, 0.001, 0.5, '', self.ORANGE)
        re_i, self._spin_enc_iris  = self._slider_row('Iris',  0.0, 1.0, 0.001, 0.5, '', self.ORANGE)
        self._chk_enc_iris = QCheckBox('Include iris encoder')
        self._chk_enc_iris.setChecked(False)
        gb_enc_lay.addWidget(re_f); gb_enc_lay.addWidget(re_z); gb_enc_lay.addWidget(re_i)
        gb_enc_lay.addWidget(self._chk_enc_iris)
        lay.addWidget(gb_enc)

        # Aperture & optics
        gb_ap, gb_ap_lay = self._group('Aperture & Optics')
        self._spin_fstop = self._spinbox(0.7, 64.0, 2.8, 0.1, 1, 'f/', color=self.PURPLE)
        self._spin_tstop = self._spinbox(0.7, 64.0, 3.2, 0.1, 1, 'T/', color=self.PURPLE)
        self._chk_fstop  = QCheckBox('Include f-stop')
        self._chk_tstop  = QCheckBox('Include t-stop')
        self._chk_fstop.setChecked(True)
        self._chk_tstop.setChecked(True)
        gb_ap_lay.addWidget(self._form_row('f-Stop', self._spin_fstop))
        gb_ap_lay.addWidget(self._chk_fstop)
        gb_ap_lay.addWidget(self._form_row('T-Stop', self._spin_tstop))
        gb_ap_lay.addWidget(self._chk_tstop)
        lay.addWidget(gb_ap)

        # Entrance pupil & anamorphic
        gb_ep, gb_ep_lay = self._group('Entrance Pupil & Anamorphic')
        self._spin_entrance_pupil = self._spinbox(-1.0, 1.0, 0.05, 0.001, 4, 'm', color=self.CYAN)
        self._spin_anamorphic     = self._spinbox(0.5, 4.0, 1.0, 0.05, 2, 'x', color=self.CYAN)
        self._chk_entrance_pupil  = QCheckBox('Include entrance pupil offset')
        self._chk_anamorphic      = QCheckBox('Include anamorphic squeeze')
        self._chk_entrance_pupil.setChecked(True)
        self._chk_anamorphic.setChecked(True)
        gb_ep_lay.addWidget(self._form_row('Entrance pupil', self._spin_entrance_pupil, unit='m'))
        gb_ep_lay.addWidget(self._chk_entrance_pupil)
        gb_ep_lay.addWidget(self._form_row('Anamorphic squeeze', self._spin_anamorphic, unit='x'))
        gb_ep_lay.addWidget(self._chk_anamorphic)
        lay.addWidget(gb_ep)

        lay.addStretch()
        return w

    # ── Tab: Distortion ────────────────────────────────────────────────────

    def _build_distortion_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        self._chk_distortion = QCheckBox('Include distortion coefficients in packet')
        self._chk_distortion.setChecked(False)
        lay.addWidget(self._chk_distortion)

        # Radial
        gb_r, gb_r_lay = self._group('Radial Coefficients')
        note = QLabel('Brown–Conrady model. Typically small values close to 0.')
        note.setObjectName('dim'); note.setWordWrap(True)
        gb_r_lay.addWidget(note)
        rk1, self._spin_k1 = self._slider_row('k1', -1.0, 1.0, 0.0001, 0.0, '', self.RED)
        rk2, self._spin_k2 = self._slider_row('k2', -1.0, 1.0, 0.0001, 0.0, '', self.RED)
        rk3, self._spin_k3 = self._slider_row('k3', -1.0, 1.0, 0.0001, 0.0, '', self.RED)
        gb_r_lay.addWidget(rk1); gb_r_lay.addWidget(rk2); gb_r_lay.addWidget(rk3)
        lay.addWidget(gb_r)

        # Tangential
        gb_t, gb_t_lay = self._group('Tangential Coefficients (Decentering)')
        rp1, self._spin_p1 = self._slider_row('p1', -0.1, 0.1, 0.00001, 0.0, '', self.ORANGE)
        rp2, self._spin_p2 = self._slider_row('p2', -0.1, 0.1, 0.00001, 0.0, '', self.ORANGE)
        gb_t_lay.addWidget(rp1); gb_t_lay.addWidget(rp2)
        lay.addWidget(gb_t)

        # Projection offset (principal point)
        gb_pp, gb_pp_lay = self._group('Projection Offset (Principal Point)')
        pp_note = QLabel('Offset of the optical axis from the image centre, in pixels.')
        pp_note.setObjectName('dim'); pp_note.setWordWrap(True)
        gb_pp_lay.addWidget(pp_note)
        self._chk_proj_offset = QCheckBox('Include projection offset')
        self._chk_proj_offset.setChecked(False)
        rpx, self._spin_proj_x = self._slider_row('X', -500.0, 500.0, 0.1, 0.0, 'px', self.CYAN)
        rpy, self._spin_proj_y = self._slider_row('Y', -500.0, 500.0, 0.1, 0.0, 'px', self.CYAN)
        gb_pp_lay.addWidget(self._chk_proj_offset)
        gb_pp_lay.addWidget(rpx); gb_pp_lay.addWidget(rpy)
        lay.addWidget(gb_pp)

        # Overscan
        gb_ov, gb_ov_lay = self._group('Overscan')
        ov_note = QLabel('Rendering overscan factor required to fill the frame after undistortion. 1.0 = no overscan.')
        ov_note.setObjectName('dim'); ov_note.setWordWrap(True)
        gb_ov_lay.addWidget(ov_note)
        ro, self._spin_overscan = self._slider_row('Factor', 1.0, 2.0, 0.001, 1.0, 'x', self.GREEN)
        gb_ov_lay.addWidget(ro)
        lay.addWidget(gb_ov)

        lay.addStretch()
        return w

    # ── Tab: Camera ────────────────────────────────────────────────────────

    def _build_camera_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        self._chk_camera = QCheckBox('Include camera block in packet')
        self._chk_camera.setChecked(True)
        lay.addWidget(self._chk_camera)

        # Identity
        gb_id, gb_id_lay = self._group('Camera Identity')

        def _edit(placeholder, default='', width=200):
            e = QLineEdit(default)
            e.setPlaceholderText(placeholder)
            e.setFixedWidth(width)
            return e

        self._edit_make   = _edit('e.g. ARRI', 'ARRI')
        self._edit_model  = _edit('e.g. ALEXA 35', 'ALEXA 35')
        self._edit_serial = _edit('Serial number', 'SIM-001')
        self._edit_label  = _edit('e.g. A-Camera', 'A')
        gb_id_lay.addWidget(self._form_row('Make',          self._edit_make))
        gb_id_lay.addWidget(self._form_row('Model',         self._edit_model))
        gb_id_lay.addWidget(self._form_row('Serial number', self._edit_serial))
        gb_id_lay.addWidget(self._form_row('Label',         self._edit_label))
        lay.addWidget(gb_id)

        # Sensor
        gb_s, gb_s_lay = self._group('Sensor')
        rs_w, self._spin_sensor_w = self._slider_row('Width',  1.0, 70.0, 0.01, 36.0, 'mm', self.YELLOW)
        rs_h, self._spin_sensor_h = self._slider_row('Height', 1.0, 50.0, 0.01, 24.0, 'mm', self.YELLOW)
        gb_s_lay.addWidget(rs_w); gb_s_lay.addWidget(rs_h)
        lay.addWidget(gb_s)

        # Resolution
        gb_res, gb_res_lay = self._group('Active Resolution (pixels)')
        self._spin_res_w = self._intbox(1, 16384, 4096, 'px', 130)
        self._spin_res_h = self._intbox(1, 16384, 3072, 'px', 130)
        self._spin_pixel_ar = self._spinbox(0.5, 4.0, 1.0, 0.001, 3, 'x', color=self.CYAN)
        gb_res_lay.addWidget(self._form_row('Width',              self._spin_res_w))
        gb_res_lay.addWidget(self._form_row('Height',             self._spin_res_h))
        gb_res_lay.addWidget(self._form_row('Pixel aspect ratio', self._spin_pixel_ar))
        lay.addWidget(gb_res)

        # Exposure
        gb_ex, gb_ex_lay = self._group('Exposure')
        self._spin_iso          = self._intbox(50, 409600, 800, 'ISO', 130)
        self._spin_shutter      = self._spinbox(1.0, 360.0, 180.0, 1.0, 1, '°', color=self.ORANGE)
        self._chk_iso           = QCheckBox('Include ISO')
        self._chk_shutter       = QCheckBox('Include shutter angle')
        self._chk_iso.setChecked(True)
        self._chk_shutter.setChecked(True)
        gb_ex_lay.addWidget(self._form_row('ISO',           self._spin_iso))
        gb_ex_lay.addWidget(self._chk_iso)
        gb_ex_lay.addWidget(self._form_row('Shutter angle', self._spin_shutter))
        gb_ex_lay.addWidget(self._chk_shutter)
        lay.addWidget(gb_ex)

        lay.addStretch()
        return w

    # ── Tab: Timing ────────────────────────────────────────────────────────

    def _build_timing_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # Frame rate
        gb_fps, gb_fps_lay = self._group('Frame Rate')
        self._combo_fps = QComboBox()
        for fps in ['23.976', '24', '25', '29.97', '30', '48', '50', '59.94', '60']:
            self._combo_fps.addItem(fps)
        self._combo_fps.setCurrentText('25')
        self._combo_fps.setFixedWidth(140)
        self._spin_fps_num   = self._intbox(1, 240, 25, '', 80)
        self._spin_fps_denom = self._intbox(1, 1001, 1, '', 80)
        note = QLabel('Quick select:'); note.setObjectName('dim')
        self._combo_fps.currentTextChanged.connect(self._on_fps_preset)
        fps_row = QWidget(); fps_rl = QHBoxLayout(fps_row); fps_rl.setContentsMargins(0,0,0,0); fps_rl.setSpacing(8)
        fps_rl.addWidget(note); fps_rl.addWidget(self._combo_fps); fps_rl.addStretch()
        manual_row = QWidget(); mr_l = QHBoxLayout(manual_row); mr_l.setContentsMargins(0,0,0,0); mr_l.setSpacing(8)
        mn = QLabel('Manual num/denom:'); mn.setObjectName('dim'); mn.setFixedWidth(150)
        slash = QLabel('/'); slash.setObjectName('dim')
        mr_l.addWidget(mn); mr_l.addWidget(self._spin_fps_num); mr_l.addWidget(slash); mr_l.addWidget(self._spin_fps_denom); mr_l.addStretch()
        gb_fps_lay.addWidget(fps_row)
        gb_fps_lay.addWidget(manual_row)
        lay.addWidget(gb_fps)

        # Timecode
        gb_tc, gb_tc_lay = self._group('Timecode')
        self._combo_tc_source = QComboBox()
        self._combo_tc_source.addItems(['System clock', 'Manual'])
        self._combo_tc_source.setFixedWidth(160)
        self._combo_tc_source.currentIndexChanged.connect(self._on_tc_source_changed)
        tc_src_row = QWidget(); tc_src_l = QHBoxLayout(tc_src_row); tc_src_l.setContentsMargins(0,0,0,0); tc_src_l.setSpacing(8)
        tc_lbl = QLabel('Source:'); tc_lbl.setObjectName('dim'); tc_lbl.setFixedWidth(80)
        tc_src_l.addWidget(tc_lbl); tc_src_l.addWidget(self._combo_tc_source); tc_src_l.addStretch()
        gb_tc_lay.addWidget(tc_src_row)
        manual_tc = QWidget(); mtc_l = QHBoxLayout(manual_tc); mtc_l.setContentsMargins(0,0,0,0); mtc_l.setSpacing(6)
        self._spin_tc_h = self._intbox(0, 23, 0, 'h', 70)
        self._spin_tc_m = self._intbox(0, 59, 0, 'm', 70)
        self._spin_tc_s = self._intbox(0, 59, 0, 's', 70)
        self._spin_tc_f = self._intbox(0, 119, 0, 'f', 70)
        for sb in [self._spin_tc_h, self._spin_tc_m, self._spin_tc_s, self._spin_tc_f]:
            sb.setEnabled(False)
            mtc_l.addWidget(sb)
        mtc_l.addStretch()
        gb_tc_lay.addWidget(manual_tc)
        self._chk_df = QCheckBox('Drop frame (29.97 / 59.94)')
        self._chk_df.setChecked(False)
        gb_tc_lay.addWidget(self._chk_df)
        lay.addWidget(gb_tc)

        # Timing mode
        gb_mode, gb_mode_lay = self._group('Timing Mode')
        self._combo_timing_mode = QComboBox()
        self._combo_timing_mode.addItems(['external', 'internal'])
        self._combo_timing_mode.setFixedWidth(160)
        mode_row = QWidget(); mode_rl = QHBoxLayout(mode_row); mode_rl.setContentsMargins(0,0,0,0); mode_rl.setSpacing(8)
        mode_lbl = QLabel('Mode:'); mode_lbl.setObjectName('dim'); mode_lbl.setFixedWidth(80)
        mode_rl.addWidget(mode_lbl); mode_rl.addWidget(self._combo_timing_mode); mode_rl.addStretch()
        gb_mode_lay.addWidget(mode_row)
        lay.addWidget(gb_mode)

        lay.addStretch()
        return w

    # ── Control bar ────────────────────────────────────────────────────────

    def _build_control_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName('card')
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        # Destination
        ip_lbl = QLabel('Destination:')
        ip_lbl.setObjectName('dim')
        self._edit_ip = QLineEdit('127.0.0.1')
        self._edit_ip.setFixedWidth(130)
        port_lbl = QLabel(':')
        port_lbl.setObjectName('dim')
        self._spin_port = self._intbox(1024, 65535, 55555, '', 80)
        lay.addWidget(ip_lbl)
        lay.addWidget(self._edit_ip)
        lay.addWidget(port_lbl)
        lay.addWidget(self._spin_port)

        # Send rate
        rate_lbl = QLabel('Send rate:')
        rate_lbl.setObjectName('dim')
        self._spin_send_rate = self._intbox(1, 120, 25, 'fps', 90)
        lay.addWidget(rate_lbl)
        lay.addWidget(self._spin_send_rate)

        lay.addStretch()

        # Status
        self._lbl_status = QLabel('Stopped')
        self._lbl_status.setObjectName('status')
        self._lbl_status.setStyleSheet(f'color: {self.DIM};')
        lay.addWidget(self._lbl_status)

        lay.addStretch()

        self._btn_send_one = QPushButton('Send One')
        self._btn_send_one.clicked.connect(self._send_one)
        lay.addWidget(self._btn_send_one)

        self._btn_start = QPushButton('Start Sending')
        self._btn_start.clicked.connect(self._start_sending)
        lay.addWidget(self._btn_start)

        self._btn_stop = QPushButton('Stop')
        self._btn_stop.setObjectName('stop')
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_sending)
        lay.addWidget(self._btn_stop)

        return frame

    # ── Callbacks ──────────────────────────────────────────────────────────

    def _on_fps_preset(self, text: str):
        fps_map = {
            '23.976': (24000, 1001), '24': (24, 1), '25': (25, 1),
            '29.97':  (30000, 1001), '30': (30, 1), '48': (48, 1),
            '50': (50, 1), '59.94': (60000, 1001), '60': (60, 1),
        }
        if text in fps_map:
            n, d = fps_map[text]
            self._spin_fps_num.setValue(n)
            self._spin_fps_denom.setValue(d)

    def _on_tc_source_changed(self, idx: int):
        manual = (idx == 1)
        for sb in [self._spin_tc_h, self._spin_tc_m, self._spin_tc_s, self._spin_tc_f]:
            sb.setEnabled(manual)

    # ── JSON / packet builder ──────────────────────────────────────────────

    def _get_timecode(self):
        if self._combo_tc_source.currentIndex() == 0:
            fps_num = self._spin_fps_num.value()
            fps_den = self._spin_fps_denom.value()
            fps = fps_num / fps_den if fps_den else 25
            now = datetime.now()
            f = int((now.microsecond / 1_000_000) * fps)
            return now.hour, now.minute, now.second, f
        return (self._spin_tc_h.value(), self._spin_tc_m.value(),
                self._spin_tc_s.value(), self._spin_tc_f.value())

    def _build_json(self) -> bytes:
        self._seq = (self._seq + 1) & 0xFFFF
        fps_num = self._spin_fps_num.value()
        fps_den = max(1, self._spin_fps_denom.value())
        h, m, s, f = self._get_timecode()

        tc = {
            'hours': h, 'minutes': m, 'seconds': s, 'frames': f,
            'frameRate': {'num': fps_num, 'denom': fps_den},
        }
        if self._chk_df.isChecked():
            tc['dropFrame'] = True

        payload = {
            'protocol':     {'name': 'OpenTrackIO', 'version': [1, 0, 1]},
            'sampleId':     f'urn:uuid:{uuid.uuid4()}',
            'sourceId':     f'urn:uuid:{self._source_id}',
            'sourceNumber': 1,
            'timing': {
                'mode':       self._combo_timing_mode.currentText(),
                'sampleRate': {'num': fps_num, 'denom': fps_den},
                'frameCount': self._seq,
                'timecode':   tc,
            },
            'transforms': [{
                'id': self._edit_subject.text() or 'Camera',
                'translation': {
                    'x': round(self._spin_x.value(), 6),
                    'y': round(self._spin_y.value(), 6),
                    'z': round(self._spin_z.value(), 6),
                },
                'rotation': {
                    'pan':  round(self._spin_pan.value(),  6),
                    'tilt': round(self._spin_tilt.value(), 6),
                    'roll': round(self._spin_roll.value(), 6),
                },
            }],
        }

        # ── Lens ──
        lens = {
            'encoders': {
                'focus': round(self._spin_enc_focus.value(), 6),
                'zoom':  round(self._spin_enc_zoom.value(),  6),
            }
        }
        if self._chk_enc_iris.isChecked():
            lens['encoders']['iris'] = round(self._spin_enc_iris.value(), 6)
        if self._chk_focal_length.isChecked():
            fl = round(self._spin_focal_length.value(), 4)
            lens['pinholeFocalLength'] = fl
            lens['focalLength']        = fl
        if self._chk_focus_dist.isChecked():
            lens['focusDistance'] = round(self._spin_focus_dist.value(), 4)
        if self._chk_fstop.isChecked():
            lens['fStop'] = round(self._spin_fstop.value(), 2)
        if self._chk_tstop.isChecked():
            lens['tStop'] = round(self._spin_tstop.value(), 2)
        if self._chk_entrance_pupil.isChecked():
            lens['entrancePupilOffset'] = round(self._spin_entrance_pupil.value(), 6)
        if self._chk_anamorphic.isChecked():
            lens['anamorphicSqueeze'] = round(self._spin_anamorphic.value(), 4)

        # Distortion
        if self._chk_distortion.isChecked():
            dist = {
                'radial':     [round(self._spin_k1.value(), 6),
                               round(self._spin_k2.value(), 6),
                               round(self._spin_k3.value(), 6)],
                'tangential': [round(self._spin_p1.value(), 6),
                               round(self._spin_p2.value(), 6)],
                'overscan':   round(self._spin_overscan.value(), 4),
            }
            lens['distortion'] = [dist]
        if self._chk_proj_offset.isChecked():
            lens['projectionOffset'] = {
                'x': round(self._spin_proj_x.value(), 4),
                'y': round(self._spin_proj_y.value(), 4),
            }

        payload['lens'] = lens

        # ── Camera ──
        if self._chk_camera.isChecked():
            cam = {
                'make':   self._edit_make.text(),
                'model':  self._edit_model.text(),
                'serial': self._edit_serial.text(),
                'label':  self._edit_label.text(),
                'sensorWidth':  round(self._spin_sensor_w.value(), 4),
                'sensorHeight': round(self._spin_sensor_h.value(), 4),
                'resolution': {
                    'width':  self._spin_res_w.value(),
                    'height': self._spin_res_h.value(),
                },
                'pixelAspectRatio': round(self._spin_pixel_ar.value(), 4),
            }
            if self._chk_iso.isChecked():
                cam['iso'] = self._spin_iso.value()
            if self._chk_shutter.isChecked():
                cam['shutterAngle'] = round(self._spin_shutter.value(), 2)
            payload['camera'] = cam

        return json.dumps(payload, separators=(',', ':')).encode('utf-8')

    # ── Send logic ─────────────────────────────────────────────────────────

    def _send_one(self):
        try:
            payload = self._build_json()
            packet  = build_opentrackio_packet(payload, self._seq)
            ip   = self._edit_ip.text().strip() or '127.0.0.1'
            port = int(self._spin_port.value())
            self._sock.sendto(packet, (ip, port))
            self._lbl_status.setText(f'Sent #{self._seq}  {len(packet)} bytes')
            self._lbl_status.setStyleSheet(f'color: {self.GREEN};')
        except Exception as e:
            self._lbl_status.setText(f'Error: {e}')
            self._lbl_status.setStyleSheet(f'color: {self.RED};')

    def _send_packet(self):
        self._send_one()

    def _start_sending(self):
        fps = max(1, self._spin_send_rate.value())
        self._timer.start(max(1, int(1000 / fps)))
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_send_one.setEnabled(False)
        self._spin_send_rate.setEnabled(False)
        self._lbl_status.setText(f'Sending @ {fps} fps')
        self._lbl_status.setStyleSheet(f'color: {self.CYAN};')

    def _stop_sending(self):
        self._timer.stop()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_send_one.setEnabled(True)
        self._spin_send_rate.setEnabled(True)
        self._lbl_status.setText('Stopped')
        self._lbl_status.setStyleSheet(f'color: {self.DIM};')

    def closeEvent(self, event):
        self._timer.stop()
        self._sock.close()
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName('OpenTrackIO Simulator')
    window = OpenTrackIOSimulator()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
