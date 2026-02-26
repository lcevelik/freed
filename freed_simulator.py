#!/usr/bin/env python3
"""
FreeD Protocol Simulator
Sends FreeD D1 UDP packets to localhost for testing without a real tracker.

Version : v1.0
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

import socket
import struct
import sys
import threading

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout,
    QSlider, QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton,
    QGroupBox, QSizePolicy,
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

# Redirect stdout/stderr on Windows windowed builds
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except AttributeError:
        import os
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')


# ── FreeD Packet Builder ───────────────────────────────────────────────────

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

    # XOR checksum over bytes 0-27
    checksum = 0
    for b in pkt[:28]:
        checksum ^= b
    pkt[28] = checksum

    return bytes(pkt)


# ── Simulator GUI ──────────────────────────────────────────────────────────

class FreeDSimulator(QMainWindow):
    """PyQt6 FreeD packet simulator with Apple dark mode UI."""

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

    TARGET_HOST = '127.0.0.1'
    TARGET_PORT = 45000

    def __init__(self):
        super().__init__()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._phase = 0
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
        QFrame#card {{
            background-color: {self.CARD};
            border-radius: 12px;
            border: 1px solid {self.BORDER};
        }}
        QGroupBox {{
            background-color: {self.CARD};
            border-radius: 10px;
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
        QSlider::groove:horizontal {{
            height: 4px;
            background: {self.BORDER};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {self.FG};
            width: 16px;
            height: 16px;
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
            padding: 2px 24px 2px 6px;
            font-family: '{_FONT_MONO}';
            font-size: 12px;
        }}
        QDoubleSpinBox::up-button, QSpinBox::up-button {{
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 18px;
            background: {self.CARD};
            border-left: 1px solid {self.BORDER};
            border-top-right-radius: 6px;
        }}
        QDoubleSpinBox::down-button, QSpinBox::down-button {{
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 18px;
            background: {self.CARD};
            border-left: 1px solid {self.BORDER};
            border-bottom-right-radius: 6px;
        }}
        QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
        QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{
            background: {self.BORDER};
        }}
        QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{
            width: 8px; height: 8px;
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-bottom: 5px solid {self.FG};
        }}
        QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{
            width: 8px; height: 8px;
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {self.FG};
        }}
        QCheckBox {{
            spacing: 6px;
            color: {self.FG};
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
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
        QPushButton:hover {{
            background-color: #5ac8fa;
        }}
        QPushButton#stop {{
            background-color: {self.RED};
            color: #ffffff;
        }}
        QPushButton#stop:hover {{
            background-color: #ff6961;
        }}
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
        QLabel#header {{
            color: {self.FG};
            font-size: 20px;
            font-weight: 700;
        }}
        QLabel#status {{
            font-family: '{_FONT_MONO}';
            font-size: 12px;
        }}
        """

    # ── UI Construction ────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle('FreeD Simulator v1.0')
        self.resize(760, 700)
        self.setMinimumSize(680, 580)
        self.setStyleSheet(self._stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # Main grid: rotation | position
        grid = QGridLayout()
        grid.setSpacing(10)
        grid.addWidget(self._build_rotation_group(), 0, 0)
        grid.addWidget(self._build_position_group(), 0, 1)
        root.addLayout(grid)

        # Lens row
        lens_row = QHBoxLayout()
        lens_row.setSpacing(10)
        lens_row.addWidget(self._build_zoom_group())
        lens_row.addWidget(self._build_focus_group())
        root.addLayout(lens_row)

        # Genlock + settings row
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)
        bottom_row.addWidget(self._build_genlock_group())
        bottom_row.addWidget(self._build_settings_group())
        root.addLayout(bottom_row)

        # Control bar
        root.addWidget(self._build_control_bar())

    # ── Slider helpers ─────────────────────────────────────────────────────

    def _make_slider_row(self, label: str, min_v: float, max_v: float,
                         step: float, default: float, unit: str,
                         color: str) -> tuple:
        """Returns (widget_row QWidget, spinbox QDoubleSpinBox, slider QSlider)."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        lbl = QLabel(label)
        lbl.setObjectName('dim')
        lbl.setFixedWidth(40)
        lay.addWidget(lbl)

        # Scale factor: we store float values in a QSlider by multiplying
        scale = int(1 / step) if step < 1 else 1
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(int(min_v * scale), int(max_v * scale))
        sl.setValue(int(default * scale))
        lay.addWidget(sl)

        spin = QDoubleSpinBox()
        spin.setRange(min_v, max_v)
        spin.setSingleStep(step)
        spin.setDecimals(max(0, len(str(step).rstrip('0').split('.')[-1])) if '.' in str(step) else 0)
        spin.setValue(default)
        spin.setSuffix(f' {unit}')
        spin.setFixedWidth(110)
        spin.setStyleSheet(f'QDoubleSpinBox {{ color: {color}; }}')
        lay.addWidget(spin)

        # Sync slider ↔ spinbox
        def sl_changed(v):
            spin.blockSignals(True)
            spin.setValue(v / scale)
            spin.blockSignals(False)

        def spin_changed(v):
            sl.blockSignals(True)
            sl.setValue(int(v * scale))
            sl.blockSignals(False)

        sl.valueChanged.connect(sl_changed)
        spin.valueChanged.connect(spin_changed)

        return row, spin, sl

    def _group_box(self, title: str) -> tuple:
        """Returns (QGroupBox, QVBoxLayout inside it)."""
        gb = QGroupBox(title)
        lay = QVBoxLayout(gb)
        lay.setSpacing(4)
        return gb, lay

    # ── Rotation group ─────────────────────────────────────────────────────

    def _build_rotation_group(self) -> QGroupBox:
        gb, lay = self._group_box('Rotation')

        row_pan, self._spin_pan, _ = self._make_slider_row(
            'Pan', -180.0, 180.0, 0.1, 0.0, '°', self.GREEN)
        row_tilt, self._spin_tilt, _ = self._make_slider_row(
            'Tilt', -90.0, 90.0, 0.1, 0.0, '°', self.GREEN)
        row_roll, self._spin_roll, _ = self._make_slider_row(
            'Roll', -180.0, 180.0, 0.1, 0.0, '°', self.GREEN)

        lay.addWidget(row_pan)
        lay.addWidget(row_tilt)
        lay.addWidget(row_roll)
        return gb

    # ── Position group ─────────────────────────────────────────────────────

    def _build_position_group(self) -> QGroupBox:
        gb, lay = self._group_box('Position')

        row_x, self._spin_x, _ = self._make_slider_row(
            'X', -50.0, 50.0, 0.01, 0.0, 'm', self.CYAN)
        row_y, self._spin_y, _ = self._make_slider_row(
            'Y', -50.0, 50.0, 0.01, 0.0, 'm', self.CYAN)
        row_z, self._spin_z, _ = self._make_slider_row(
            'Z', -50.0, 50.0, 0.01, 0.0, 'm', self.CYAN)

        lay.addWidget(row_x)
        lay.addWidget(row_y)
        lay.addWidget(row_z)
        return gb

    # ── Zoom group ─────────────────────────────────────────────────────────

    def _build_zoom_group(self) -> QGroupBox:
        gb, lay = self._group_box('Zoom')

        self._chk_zoom_nodata = QCheckBox('No data (raw = 0)')
        self._chk_zoom_nodata.stateChanged.connect(self._on_zoom_nodata)
        lay.addWidget(self._chk_zoom_nodata)

        row_zoom, self._spin_zoom, self._sl_zoom = self._make_slider_row(
            'mm', 1.0, 300.0, 0.1, 24.0, 'mm', self.YELLOW)
        lay.addWidget(row_zoom)
        return gb

    def _on_zoom_nodata(self, state):
        enabled = not bool(state)
        self._spin_zoom.setEnabled(enabled)
        self._sl_zoom.setEnabled(enabled)

    # ── Focus group ────────────────────────────────────────────────────────

    def _build_focus_group(self) -> QGroupBox:
        gb, lay = self._group_box('Focus')

        self._chk_focus_nodata = QCheckBox('No data (raw = 65535)')
        self._chk_focus_nodata.setChecked(True)
        self._chk_focus_nodata.stateChanged.connect(self._on_focus_nodata)
        lay.addWidget(self._chk_focus_nodata)

        row_focus, self._spin_focus, self._sl_focus = self._make_slider_row(
            'm', 0.1, 100.0, 0.01, 1.5, 'm', self.YELLOW)
        self._spin_focus.setEnabled(False)
        self._sl_focus.setEnabled(False)
        lay.addWidget(row_focus)
        return gb

    def _on_focus_nodata(self, state):
        enabled = not bool(state)
        self._spin_focus.setEnabled(enabled)
        self._sl_focus.setEnabled(enabled)

    # ── Genlock group ──────────────────────────────────────────────────────

    def _build_genlock_group(self) -> QGroupBox:
        gb, lay = self._group_box('Genlock')

        self._chk_genlock = QCheckBox('Genlock ON  (cycles phase counter)')
        self._chk_genlock.setChecked(True)
        lay.addWidget(self._chk_genlock)

        info = QLabel('Phase counter increments each packet\nwhen ON, stays 0x0 when OFF')
        info.setObjectName('dim')
        info.setWordWrap(True)
        lay.addWidget(info)
        return gb

    # ── Settings group ─────────────────────────────────────────────────────

    def _build_settings_group(self) -> QGroupBox:
        gb, lay = self._group_box('Settings')

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._spin_cam_id = QSpinBox()
        self._spin_cam_id.setRange(0, 255)
        self._spin_cam_id.setValue(1)
        self._spin_cam_id.setFixedWidth(100)

        self._spin_fps = QSpinBox()
        self._spin_fps.setRange(1, 60)
        self._spin_fps.setValue(25)
        self._spin_fps.setSuffix(' fps')
        self._spin_fps.setFixedWidth(100)

        lbl_cam = QLabel('Camera ID')
        lbl_cam.setObjectName('dim')
        lbl_fps = QLabel('Send rate')
        lbl_fps.setObjectName('dim')

        form.addRow(lbl_cam, self._spin_cam_id)
        form.addRow(lbl_fps, self._spin_fps)
        lay.addLayout(form)
        return gb

    # ── Control bar ────────────────────────────────────────────────────────

    def _build_control_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName('card')
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

        self._btn_send_one = QPushButton('Send One Packet')
        self._btn_send_one.clicked.connect(self._send_one)
        lay.addWidget(self._btn_send_one)

        lay.addStretch()

        self._btn_start = QPushButton('Start Sending')
        self._btn_start.clicked.connect(self._start_sending)
        lay.addWidget(self._btn_start)

        self._btn_stop = QPushButton('Stop')
        self._btn_stop.setObjectName('stop')
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_sending)
        lay.addWidget(self._btn_stop)

        return frame

    # ── Packet building & sending ──────────────────────────────────────────

    def _current_packet(self) -> bytes:
        self._phase = (self._phase + 1) & 0x0F if self._chk_genlock.isChecked() else 0
        return build_freed_packet(
            camera_id    = self._spin_cam_id.value(),
            pan_deg      = self._spin_pan.value(),
            tilt_deg     = self._spin_tilt.value(),
            roll_deg     = self._spin_roll.value(),
            x_m          = self._spin_x.value(),
            y_m          = self._spin_y.value(),
            z_m          = self._spin_z.value(),
            zoom_mm      = self._spin_zoom.value(),
            zoom_no_data = self._chk_zoom_nodata.isChecked(),
            focus_m      = self._spin_focus.value(),
            focus_no_data= self._chk_focus_nodata.isChecked(),
            genlock_on   = self._chk_genlock.isChecked(),
            phase_counter= self._phase,
        )

    def _send_one(self):
        pkt = self._current_packet()
        self._sock.sendto(pkt, (self.TARGET_HOST, self.TARGET_PORT))

    def _send_packet(self):
        self._send_one()

    def _start_sending(self):
        self._sending = True
        fps = self._spin_fps.value()
        interval_ms = max(1, int(1000 / fps))
        self._timer.start(interval_ms)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_send_one.setEnabled(False)
        self._spin_fps.setEnabled(False)

    def _stop_sending(self):
        self._sending = False
        self._timer.stop()
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_send_one.setEnabled(True)
        self._spin_fps.setEnabled(True)

    def closeEvent(self, event):
        self._timer.stop()
        self._sock.close()
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName('FreeD Simulator')
    window = FreeDSimulator()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
