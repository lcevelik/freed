#!/usr/bin/env python3
"""
FreeD Dashboard
Receives, parses, and analyses FreeD D1 camera tracking data over UDP.
Provides forwarding, TC injection, and OpenTrackIO output.

Version : v2.0.0
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

__version__   = 'v2.0.0'
__author__    = 'Libor Cevelik'
__copyright__ = 'Copyright (c) 2026 Libor Cevelik'

import ctypes
import json
import os
import socket
import sys
import threading
import time
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QSpinBox, QPushButton, QLineEdit, QComboBox, QTextEdit, QScrollArea,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QColor
import numpy as np
from src.protocol import (FreeDParser, FreeDReceiver, FreeDReceiverGUI, UdpListener,
                          PROTO_FREED, PROTO_OTI, PROTO_NAMES)
from src.opentrackio import (OpenTrackIOSender, OpenTrackIOParser, oti_to_freed_packet,
                             oti_timecode, oti_device_label, oti_camera_transform)
from src.ltc_reader import BluefishLTCReader
from src.forwarder import FreeDForwarder

from src.ui_utils import FONT_MONO as _FONT_MONO, FONT_SANS as _FONT_SANS, configure_stdout
configure_stdout()

# Set Windows timer resolution to 1ms so packet interval measurements
# reflect the actual source signal rather than the default 15.6ms OS tick.
if sys.platform == 'win32':
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
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
        self._cached_ip_str  = None
        self.forwarder       = FreeDForwarder()
        # Per-protocol state (counters, jitter/noise histories, latest packet).
        # Sockets live in UdpListener; these objects never open their own.
        self.receiver        = self._new_state()      # FreeD input
        self.oti_state       = self._new_state()      # OpenTrackIO input, converted to FreeD form
        self.oti_parser      = OpenTrackIOParser()
        self.listeners       = {}                     # port -> UdpListener
        self._listen_errors  = {}                     # port -> bind error text
        self._last_rx        = {}                     # proto -> perf_counter time of last datagram
        self._sender_labels  = {}                     # addr -> device label
        self._relay_blocked  = False
        self._active_source  = self._resolve_active_source()
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
        self._check_relay_loop()
        self._start_listeners()
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

        self.lbl_src_badge = QLabel('FreeD')
        self.lbl_src_badge.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        self.lbl_src_badge.setFixedHeight(22)
        layout.addWidget(self.lbl_src_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._hdr_src_combo = QComboBox()
        self._hdr_src_combo.setStyleSheet(self._combo_qss())
        self._hdr_src_combo.setToolTip('Source shown on Dashboard / Jitter and sent to the outputs')
        self._hdr_src_combo.addItem('Show FreeD', PROTO_FREED)
        self._hdr_src_combo.addItem('Show OpenTrackIO', PROTO_OTI)
        self._hdr_src_combo.currentIndexChanged.connect(self._on_hdr_source_changed)
        layout.addWidget(self._hdr_src_combo)

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

        oti = QWidget()
        self._build_oti_tab(oti)
        self._oti_tab_index = tabs.addTab(oti, '  OpenTrackIO  ')
        self._tabs = tabs

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
        self.lbl_port.setText(self._ports_str())
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
        outer = QVBoxLayout(parent)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sub = QTabWidget()
        sub.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background-color: {self.BG}; }}
            QTabBar::tab {{
                background-color: {self.BG}; color: {self.DIM};
                padding: 6px 16px; border: none; font-size: 12px;
                font-family: {_FONT_SANS};
            }}
            QTabBar::tab:selected {{ color: {self.FG}; border-bottom: 2px solid {self.CYAN}; }}
            QTabBar::tab:hover {{ color: {self.FG}; }}
        """)
        outer.addWidget(sub)

        monitor_page = QWidget()
        monitor_page.setStyleSheet(f'background-color: {self.BG};')
        sub.addTab(monitor_page, 'Monitor')

        ref_page = QWidget()
        ref_page.setStyleSheet(f'background-color: {self.BG};')
        self._build_jitter_reference(ref_page)
        sub.addTab(ref_page, 'Reference')

        # rest of monitor tab built into monitor_page
        parent = monitor_page
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

        # ── Position noise ────────────────────────────────────────────
        pos_frame = QFrame(); pos_frame.setObjectName('card')
        pos_outer = QVBoxLayout(pos_frame)
        pos_outer.setContentsMargins(14, 10, 14, 12); pos_outer.setSpacing(6)
        pos_title = QLabel('POSITION NOISE  (std dev over last 500 packets)   ● ideal <0.1mm   ● accept <0.5mm   ● problem >1mm')
        pos_title.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        pos_title.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        pos_outer.addWidget(pos_title)

        pos_row = QWidget(); pos_row.setStyleSheet('background: transparent;')
        pos_row_l = QHBoxLayout(pos_row); pos_row_l.setContentsMargins(0,0,0,0); pos_row_l.setSpacing(10)
        self._noise_pos_labels = {}
        for axis in ('X', 'Y', 'Z'):
            f = QFrame(); f.setObjectName('card')
            vb = QVBoxLayout(f); vb.setContentsMargins(10,8,10,10); vb.setSpacing(2)
            lt = QLabel(axis)
            lt.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
            lt.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            lt.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lv = QLabel('---')
            lv.setFont(QFont(_FONT_MONO, 14, QFont.Weight.Bold))
            lv.setStyleSheet(f'color: {self.FG}; background: transparent;')
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ls = QLabel('mm std dev')
            ls.setFont(QFont(_FONT_SANS, 8))
            ls.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            ls.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vb.addWidget(lt); vb.addWidget(lv); vb.addWidget(ls)
            pos_row_l.addWidget(f)
            self._noise_pos_labels[axis] = lv
        pos_outer.addWidget(pos_row)
        layout.addWidget(pos_frame)

        # ── Rotation noise ────────────────────────────────────────────
        rot_frame = QFrame(); rot_frame.setObjectName('card')
        rot_outer = QVBoxLayout(rot_frame)
        rot_outer.setContentsMargins(14, 10, 14, 12); rot_outer.setSpacing(6)
        rot_title = QLabel('ROTATION NOISE  (std dev over last 500 packets)   ● ideal <0.01°   ● accept <0.05°   ● problem >0.1°')
        rot_title.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
        rot_title.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        rot_outer.addWidget(rot_title)

        rot_row = QWidget(); rot_row.setStyleSheet('background: transparent;')
        rot_row_l = QHBoxLayout(rot_row); rot_row_l.setContentsMargins(0,0,0,0); rot_row_l.setSpacing(10)
        self._noise_rot_labels = {}
        for axis in ('Pan', 'Tilt', 'Roll'):
            f = QFrame(); f.setObjectName('card')
            vb = QVBoxLayout(f); vb.setContentsMargins(10,8,10,10); vb.setSpacing(2)
            lt = QLabel(axis)
            lt.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
            lt.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            lt.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lv = QLabel('---')
            lv.setFont(QFont(_FONT_MONO, 14, QFont.Weight.Bold))
            lv.setStyleSheet(f'color: {self.FG}; background: transparent;')
            lv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ls = QLabel('° std dev')
            ls.setFont(QFont(_FONT_SANS, 8))
            ls.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            ls.setAlignment(Qt.AlignmentFlag.AlignCenter)
            vb.addWidget(lt); vb.addWidget(lv); vb.addWidget(ls)
            rot_row_l.addWidget(f)
            self._noise_rot_labels[axis] = lv
        rot_outer.addWidget(rot_row)
        layout.addWidget(rot_frame)

        layout.addStretch()

    def _build_jitter_reference(self, parent: QWidget):
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)

        doc = QTextEdit()
        doc.setReadOnly(True)
        doc.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self.CARD};
                color: {self.FG};
                border: 1px solid {self.BORDER};
                border-radius: 10px;
                padding: 14px;
                font-family: {_FONT_SANS};
                font-size: 12px;
            }}
            QScrollBar:vertical {{
                background: {self.BG}; width: 8px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.BORDER}; border-radius: 4px;
            }}
        """)
        doc.setHtml(f"""
<style>
  body   {{ color: {self.FG}; font-family: {_FONT_SANS}; font-size: 12px; line-height: 1.6; }}
  h1     {{ color: {self.CYAN}; font-size: 15px; margin-top: 0; margin-bottom: 4px; }}
  h2     {{ color: {self.FG}; font-size: 13px; margin-top: 18px; margin-bottom: 4px; border-bottom: 1px solid {self.BORDER}; padding-bottom: 3px; }}
  h3     {{ color: {self.YELLOW}; font-size: 12px; margin-top: 12px; margin-bottom: 2px; }}
  p      {{ color: {self.FG}; margin: 4px 0; }}
  .dim   {{ color: {self.DIM}; }}
  .good  {{ color: {self.GREEN}; font-weight: bold; }}
  .warn  {{ color: {self.YELLOW}; font-weight: bold; }}
  .bad   {{ color: {self.RED}; font-weight: bold; }}
  .note  {{ color: {self.ORANGE}; }}
  table  {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
  th     {{ color: {self.DIM}; font-size: 11px; text-align: left; padding: 4px 8px;
            border-bottom: 1px solid {self.BORDER}; }}
  td     {{ padding: 4px 8px; border-bottom: 1px solid {self.BORDER}; color: {self.FG}; }}
  tr:last-child td {{ border-bottom: none; }}
  ul     {{ margin: 4px 0 4px 16px; padding: 0; }}
  li     {{ margin: 2px 0; }}
</style>

<h1>FreeD Jitter — Reference Guide</h1>
<p class="dim">Professional thresholds for virtual production and LED volume work.</p>

<h2>Packet Timing Jitter</h2>
<table>
  <tr><th>Rating</th><th>Threshold</th><th>Effect</th></tr>
  <tr><td><span class="good">Ideal</span></td><td>&lt; 1 ms</td><td>No visible impact</td></tr>
  <tr><td><span class="warn">Acceptable</span></td><td>1 – 3 ms</td><td>Safe, monitor on fast pans</td></tr>
  <tr><td><span class="bad">Problematic</span></td><td>&gt; 5 ms</td><td>Visible judder on LED wall, especially fast pans</td></tr>
</table>

<h2>Position Data (X / Y / Z)</h2>
<table>
  <tr><th>Rating</th><th>Variation at rest</th><th>Effect</th></tr>
  <tr><td><span class="good">Ideal</span></td><td>&lt; 0.1 mm</td><td>No visible impact</td></tr>
  <tr><td><span class="warn">Acceptable</span></td><td>&lt; 0.5 mm</td><td>Minor, generally invisible</td></tr>
  <tr><td><span class="bad">Problematic</span></td><td>&gt; 1 mm</td><td>Swimming / floating on composited CG elements</td></tr>
</table>

<h2>Rotation Data (Pan / Tilt / Roll)</h2>
<table>
  <tr><th>Rating</th><th>Variation at rest</th><th>Effect</th></tr>
  <tr><td><span class="good">Ideal</span></td><td>&lt; 0.01°</td><td>No visible impact</td></tr>
  <tr><td><span class="warn">Acceptable</span></td><td>&lt; 0.05°</td><td>Minor, watch on wide lenses</td></tr>
  <tr><td><span class="bad">Problematic</span></td><td>&gt; 0.1°</td><td>Visible horizon drift, especially wide lenses</td></tr>
</table>

<h2>Zoom / Focus</h2>
<p>Less sensitive — 0.5–1% variance is generally fine unless doing tight macro work.</p>

<h2>Typical Jitter by Source</h2>
<table>
  <tr><th>Source</th><th>Typical Jitter</th><th>Notes</th></tr>
  <tr><td>Encoded tracking (Mo-Sys, Ncam)</td><td>~0.5–1 ms</td><td>Best case via dedicated UDP stream</td></tr>
  <tr><td>FreeD over serial (RS-422)</td><td>1–2 ms</td><td>Hardware-limited but stable</td></tr>
  <tr><td>FreeD over UDP (network)</td><td>1–5 ms</td><td>Switch-dependent; use unmanaged or QoS-configured</td></tr>
  <tr><td>LiveLink bridge (UE5)</td><td>+1–3 ms added</td><td>LiveLink adds its own buffering</td></tr>
</table>

<h2>Key Considerations</h2>
<ul>
  <li><span class="note">Genlock is the bigger variable</span> — if your FreeD source isn't locked to the same sync signal as your camera/LED system, even 1 ms jitter can look like more because it's frame-phase-inconsistent.</li>
  <li>LiveLink Subject in UE5 buffers FreeD data — tune buffer size in LiveLink settings to smooth jitter at the cost of latency.</li>
  <li>nDisplay rendering latency matters more than raw FreeD jitter — the two need to be tuned together.</li>
  <li>At 24fps, one frame = ~41.7 ms — anything under 5 ms jitter is within a single frame and manageable with buffer compensation.</li>
  <li><span class="note">Rule of thumb:</span> if it's invisible at rest on a locked-off shot, it's acceptable. Static jitter above 0.5 mm or 0.05° will almost always be visible on a composited CG floor or horizon line.</li>
</ul>

<h2>Common Causes &amp; Fixes</h2>

<h3>1. Network / UDP Transport</h3>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>Shared network with other traffic</td><td>Dedicated VLAN or isolated switch for tracking data</td></tr>
  <tr><td>Managed switch with STP/IGMP overhead</td><td>Use unmanaged switch, or disable STP on tracking ports</td></tr>
  <tr><td>Wrong switch QoS settings</td><td>Tag FreeD UDP traffic with highest QoS priority (DSCP EF)</td></tr>
  <tr><td>Long cable runs with cheap switches</td><td>Stay under 3 hops; use Cat6 point-to-point where possible</td></tr>
  <tr><td>Wireless anywhere in the chain</td><td>Eliminate entirely — FreeD must be wired end-to-end</td></tr>
</table>

<h3>2. Tracking System Hardware</h3>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>Optical/encoder head vibration</td><td>Check rig mounting — loose head bolts are a common culprit</td></tr>
  <tr><td>Mechanical encoder slop on pan/tilt</td><td>Re-calibrate zero point; check encoder coupling for backlash</td></tr>
  <tr><td>IR interference (Vicon, Ncam)</td><td>Check IR reflector cleanliness; eliminate competing IR sources (LED wall leakage, windows)</td></tr>
  <tr><td>Camera cable drag on inertial sensors</td><td>Reroute cables so they don't pull on the head</td></tr>
  <tr><td>Thermal drift</td><td>Allow 15–20 min warm-up before calibration on IMU-based systems</td></tr>
</table>

<h3>3. Serial / RS-422 Source</h3>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>Cable too long</td><td>Keep RS-422 under 300 m; use proper termination (120Ω)</td></tr>
  <tr><td>Ground loop on serial line</td><td>Use isolated RS-422 converter</td></tr>
  <tr><td>Baud rate mismatch causing re-sync</td><td>Confirm both ends locked to same baud (usually 38400 for FreeD)</td></tr>
  <tr><td>USB-to-serial adapter</td><td>Replace with dedicated RS-422 PCIe card — USB adds 2–8 ms variable latency</td></tr>
</table>

<h3>4. FreeD Relay / Bridge Software</h3>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>Python relay running on shared CPU</td><td>Pin process to isolated CPU core; set high process priority</td></tr>
  <tr><td>Relay on same machine as UE5</td><td>Move relay to dedicated small PC or use hardware converter</td></tr>
  <tr><td>Virtual NIC or VPN active on relay machine</td><td>Disable all non-essential network adapters</td></tr>
  <tr><td>OS scheduling interrupts (Windows)</td><td>Enable High Performance power plan; disable CPU parking; consider MMCSS tuning</td></tr>
</table>

<h3>5. Unreal Engine / LiveLink</h3>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>LiveLink buffer too small (dropping packets)</td><td>Increase LiveLink subject buffer size in Project Settings</td></tr>
  <tr><td>LiveLink buffer too large (added latency)</td><td>Reduce buffer — find minimum that eliminates visible jitter</td></tr>
  <tr><td>UE5 running below target framerate</td><td>Reduce scene complexity or use nDisplay load balancing</td></tr>
  <tr><td>LiveLink running on game thread</td><td>Use LiveLink Hub as external process to offload from game thread</td></tr>
  <tr><td>Wrong timecode source in LiveLink</td><td>Ensure LiveLink timecode provider matches your genlock master</td></tr>
</table>

<h3>6. Genlock / Sync Mismatch</h3>
<p class="dim">Most subtle and commonly misdiagnosed — data looks clean but appears jittery because it's phase-inconsistent with the render frame.</p>
<table>
  <tr><th>Cause</th><th>Fix</th></tr>
  <tr><td>FreeD source not locked to house sync</td><td>Lock tracking system output to same sync reference as camera</td></tr>
  <tr><td>Sync not reaching tracking computer</td><td>Verify tri-level or blackburst reaching the tracking workstation</td></tr>
  <tr><td>FreeD packet rate ≠ camera frame rate</td><td>FreeD should output at 2× or exact frame rate (e.g., 48 Hz packets for 24 fps)</td></tr>
  <tr><td>Mixed sync domains</td><td>Single sync master — everything downstream</td></tr>
</table>

<h2>Diagnosis Workflow</h2>
<ol style="margin: 4px 0 4px 16px; padding: 0; color: {self.FG};">
  <li>Lock off camera completely (sandbag the head)</li>
  <li>Log raw FreeD UDP packets with Wireshark or a simple Python listener</li>
  <li>Plot X/Y/Z and pan/tilt over 10 seconds at rest</li>
  <li>If data is clean → jitter is in UE/LiveLink pipeline</li>
  <li>If data is noisy → jitter is upstream (tracking HW or transport)</li>
  <li>Check packet interval consistency — irregular timing = network/serial issue</li>
  <li>Check value noise floor — random LSB flicker = encoder/IMU issue</li>
</ol>
        """)
        layout.addWidget(doc)

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

        self._build_network_page(net_layout)
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

        fwd_note = QLabel('OpenTrackIO input is converted to FreeD D1 for these destinations '
                          '(29 bytes, no timecode). TC injection applies to FreeD input only.')
        fwd_note.setWordWrap(True)
        fwd_note.setFont(QFont(_FONT_SANS, 9))
        fwd_note.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        dest_outer.addWidget(fwd_note)

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

        oti_note = QLabel('FreeD input → generated from FreeD   ·   '
                          'OpenTrackIO input → relayed unchanged')
        oti_note.setFont(QFont(_FONT_SANS, 9))
        oti_note.setStyleSheet(f'color: {self.DIM}; background: transparent; border: none;')
        oti_outer.addWidget(oti_note)

        self._oti_relay_warn = QLabel('⚠ Relay paused — this output points back at an OpenTrackIO '
                                      'listen port on this machine (would loop).')
        self._oti_relay_warn.setWordWrap(True)
        self._oti_relay_warn.setFont(QFont(_FONT_SANS, 9))
        self._oti_relay_warn.setStyleSheet(f'color: {self.ORANGE}; background: transparent; border: none;')
        self._oti_relay_warn.setVisible(False)
        oti_outer.addWidget(self._oti_relay_warn)


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
            self._check_relay_loop()

    def _on_oti_port_changed(self):
        try:
            p = int(self._oti_port.text())
            if 1 <= p <= 65535:
                self.oti_sender.port = p
                self.forwarder.oti_port = p
                self.forwarder.save_config()
                self._check_relay_loop()
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
    # Input — listeners, routing, active source (receive threads)
    # ------------------------------------------------------------------

    @staticmethod
    def _new_state() -> FreeDReceiverGUI:
        """Counters + jitter/noise histories for one input protocol (no socket)."""
        return FreeDReceiverGUI(host='0.0.0.0', port=0, ignore_checksum=True,
                                timecode_fps=24.0, convert_units=True,
                                clear_screen=False, debug=False)

    def _resolve_active_source(self) -> str:
        mode = self.forwarder.input_mode
        if mode == 'freed':
            return PROTO_FREED
        if mode == 'oti':
            return PROTO_OTI
        if mode == 'both':
            src = self.forwarder.active_source
            return src if src in (PROTO_FREED, PROTO_OTI) else PROTO_FREED
        return getattr(self, '_active_source', PROTO_FREED)   # auto — follows traffic

    def _listen_plan(self) -> dict:
        """port -> set of protocols accepted on it, for the current input mode."""
        f, mode = self.forwarder, self.forwarder.input_mode
        if mode == 'freed':
            return {f.listen_port: {PROTO_FREED}}
        if mode == 'oti':
            return {f.oti_listen_port: {PROTO_OTI}}
        if mode == 'auto':
            return {f.listen_port: {PROTO_FREED, PROTO_OTI}}
        plan = {}
        plan.setdefault(f.listen_port, set()).add(PROTO_FREED)
        plan.setdefault(f.oti_listen_port, set()).add(PROTO_OTI)
        return plan

    def _ports_str(self) -> str:
        return ' / '.join(str(p) for p in sorted(self._listen_plan()))

    def _sender_filter(self) -> dict:
        return {PROTO_FREED: self.forwarder.freed_sender_ip,
                PROTO_OTI:   self.forwarder.oti_sender_ip}

    def _start_listeners(self):
        self._stop_listeners()
        self._listen_errors = {}
        handlers = {PROTO_FREED: self._on_freed_datagram, PROTO_OTI: self._on_oti_datagram}
        for port, accept in self._listen_plan().items():
            lst = UdpListener(port, accept, handlers)
            lst.ip_filter = self._sender_filter()
            try:
                lst.start()
            except OSError as e:
                self._listen_errors[port] = str(e)
                continue
            self.listeners[port] = lst

    def _stop_listeners(self):
        for lst in self.listeners.values():
            lst.stop()
        self.listeners = {}

    def _restart_listeners(self):
        self._start_listeners()
        self.lbl_port.setText(self._ports_str())
        self._check_relay_loop()
        self._update_settings_status()

    def _view_state(self) -> FreeDReceiverGUI:
        """State object behind the Dashboard / Packet Map / Jitter tabs."""
        return self.oti_state if self._active_source == PROTO_OTI else self.receiver

    def _note_rx(self, proto: str, t: float):
        self._last_rx[proto] = t
        if self.forwarder.input_mode == 'auto' and self._active_source != proto:
            cur = self._last_rx.get(self._active_source)
            if cur is None or t - cur > 1.0:     # current source silent for 1 s — follow the new one
                self._active_source = proto

    def _on_freed_datagram(self, data: bytes, addr: tuple, recv_time: float):
        """Receive thread: FreeD D1 datagram."""
        self._note_rx(PROTO_FREED, recv_time)
        r = self.receiver
        parsed = r.parser.parse(data)
        if not parsed:
            return
        self._sender_labels[addr] = f"FreeD · cam {parsed['camera_id']}"
        r.display_data(parsed, addr, recv_time=recv_time)
        if self._active_source == PROTO_FREED:
            self.forwarder.forward(parsed['raw_bytes'], self.ltc_reader)
            self._on_parsed_packet(parsed)

    def _on_oti_datagram(self, data: bytes, addr: tuple, recv_time: float):
        """Receive thread: one OpenTrackIO datagram (possibly one segment of a sample)."""
        self._note_rx(PROTO_OTI, recv_time)
        active = self._active_source == PROTO_OTI
        if active and not self._relay_blocked:
            self.oti_sender.relay(data)
        sample = self.oti_parser.feed(data, addr, recv_time)
        if sample is None:
            return
        self._sender_labels[addr] = oti_device_label(sample)
        pkt = oti_to_freed_packet(sample)
        # Display copy carries the camera TC in the extended block (bytes 29-32)
        # so the Dashboard timecode reads from OpenTrackIO; the forwarded
        # packet stays a plain 29-byte D1 without timecode.
        tc   = oti_timecode(sample)
        disp = pkt + bytes(v & 0xFF for v in tc) if tc else pkt
        s = self.oti_state
        parsed = s.parser.parse(disp)
        if parsed is None:
            return
        parsed['oti'] = sample
        s.display_data(parsed, addr, recv_time=recv_time)
        if active:
            self.forwarder.forward(pkt, self.ltc_reader, inject_tc=False)

    def _local_ips(self) -> set:
        if not hasattr(self, '_local_ip_cache'):
            try:
                self._local_ip_cache = {
                    info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
            except Exception:
                self._local_ip_cache = set()
        return self._local_ip_cache

    def _check_relay_loop(self):
        """Pause OTI relay if the OTI output points back at our own OTI listen port."""
        ip = (self.oti_sender.ip or '').strip()
        is_local = (ip.startswith('127.') or ip in ('localhost', '0.0.0.0')
                    or ip.endswith('.255') or ip in self._local_ips())
        oti_ports = {p for p, acc in self._listen_plan().items() if PROTO_OTI in acc}
        self._relay_blocked = is_local and self.oti_sender.port in oti_ports
        lbl = getattr(self, '_oti_relay_warn', None)
        if lbl is not None:
            lbl.setVisible(self._relay_blocked)

    # ------------------------------------------------------------------
    # Header source badge / picker
    # ------------------------------------------------------------------

    def _update_source_badge(self):
        src, mode = self._active_source, self.forwarder.input_mode
        key = (src, mode)
        if getattr(self, '_badge_key', None) == key:
            return
        self._badge_key = key
        color = self.CYAN if src == PROTO_OTI else self.GREEN
        text = PROTO_NAMES[src] + ('  ·  AUTO' if mode == 'auto' else '')
        self.lbl_src_badge.setText(text)
        self.lbl_src_badge.setStyleSheet(
            f'color: {color}; background: transparent; border: 1px solid {color};'
            f' border-radius: 8px; padding: 1px 8px;')
        self._hdr_src_combo.setVisible(mode == 'both')
        self._hdr_src_combo.blockSignals(True)
        self._hdr_src_combo.setCurrentIndex(0 if src == PROTO_FREED else 1)
        self._hdr_src_combo.blockSignals(False)

    def _on_hdr_source_changed(self, idx: int):
        proto = self._hdr_src_combo.itemData(idx)
        if proto not in (PROTO_FREED, PROTO_OTI) or self.forwarder.input_mode != 'both':
            return
        self._active_source = proto
        self.forwarder.active_source = proto
        self.forwarder.save_config()

    # ------------------------------------------------------------------
    # Settings › Network
    # ------------------------------------------------------------------

    def _combo_qss(self) -> str:
        return f"""
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

    def _spin_qss(self) -> str:
        return f"""
            QSpinBox {{
                background-color: {self.BG}; color: {self.FG};
                border: 1px solid {self.BORDER}; border-radius: 6px;
                padding: 4px 8px; font-family: {_FONT_MONO}; font-size: 13px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 18px; background-color: {self.BORDER}; border-radius: 3px;
            }}
        """

    def _build_network_page(self, net_layout: QVBoxLayout):
        f = self.forwarder

        def _title(text):
            lbl = QLabel(text)
            lbl.setFont(QFont(_FONT_SANS, 9, QFont.Weight.Bold))
            lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            return lbl

        def _row(label_text):
            w = QWidget(); w.setStyleSheet('background: transparent;')
            hl = QHBoxLayout(w); hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(10)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(140)
            lbl.setFont(QFont(_FONT_SANS, 11))
            lbl.setStyleSheet(f'color: {self.FG}; background: transparent;')
            hl.addWidget(lbl)
            return w, hl, lbl

        def _small(text):
            lbl = QLabel(text)
            lbl.setFont(QFont(_FONT_SANS, 9))
            lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
            return lbl

        # ── Input card ────────────────────────────────────────────────
        net_frame = QFrame()
        net_frame.setObjectName('card')
        net_inner = QVBoxLayout(net_frame)
        net_inner.setContentsMargins(16, 12, 16, 14)
        net_inner.setSpacing(10)
        net_inner.addWidget(_title('INPUT'))

        mode_w, mode_l, _ = _row('Input')
        self._mode_combo = QComboBox()
        self._mode_combo.setFixedWidth(230)
        self._mode_combo.setStyleSheet(self._combo_qss())
        for label, val in [('FreeD', 'freed'), ('OpenTrackIO', 'oti'),
                           ('Both  (separate ports)', 'both'), ('Auto-detect', 'auto')]:
            self._mode_combo.addItem(label, val)
        self._mode_combo.setCurrentIndex(FreeDForwarder.INPUT_MODES.index(f.input_mode))
        self._mode_combo.currentIndexChanged.connect(self._on_input_mode_changed)
        mode_l.addWidget(self._mode_combo)
        self._mode_hint = _small('')
        mode_l.addWidget(self._mode_hint, stretch=1)
        net_inner.addWidget(mode_w)

        self._freed_port_row, fp_l, self._freed_port_lbl = _row('FreeD port')
        self._freed_port_spin = QSpinBox()
        self._freed_port_spin.setRange(1024, 65535)
        self._freed_port_spin.setValue(f.listen_port)
        self._freed_port_spin.setFixedWidth(100)
        self._freed_port_spin.setStyleSheet(self._spin_qss())
        fp_l.addWidget(self._freed_port_spin)
        fp_l.addWidget(_small('accept from'))
        self._freed_sender_combo = QComboBox()
        self._freed_sender_combo.setFixedWidth(230)
        self._freed_sender_combo.setStyleSheet(self._combo_qss())
        self._freed_sender_combo.currentIndexChanged.connect(
            lambda _i: self._on_sender_combo_changed(PROTO_FREED))
        fp_l.addWidget(self._freed_sender_combo)
        fp_l.addStretch()
        net_inner.addWidget(self._freed_port_row)

        self._oti_port_row, op_l, _ = _row('OpenTrackIO port')
        self._oti_port_spin = QSpinBox()
        self._oti_port_spin.setRange(1024, 65535)
        self._oti_port_spin.setValue(f.oti_listen_port)
        self._oti_port_spin.setFixedWidth(100)
        self._oti_port_spin.setStyleSheet(self._spin_qss())
        op_l.addWidget(self._oti_port_spin)
        op_l.addWidget(_small('accept from'))
        self._oti_sender_combo = QComboBox()
        self._oti_sender_combo.setFixedWidth(230)
        self._oti_sender_combo.setStyleSheet(self._combo_qss())
        self._oti_sender_combo.currentIndexChanged.connect(
            lambda _i: self._on_sender_combo_changed(PROTO_OTI))
        op_l.addWidget(self._oti_sender_combo)
        op_l.addStretch()
        net_inner.addWidget(self._oti_port_row)

        apply_w, apply_l, _ = _row('')
        apply_btn = QPushButton('Apply ports')
        apply_btn.setFixedWidth(100)
        apply_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.CYAN}; color: #000000;
                border: none; border-radius: 6px; padding: 5px 14px;
                font-family: {_FONT_SANS}; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #5ac8fa; }}
            QPushButton:pressed {{ background-color: #0a84ff; }}
        """)
        apply_btn.clicked.connect(self._on_apply_ports)
        apply_l.addWidget(apply_btn)
        apply_l.addStretch()
        net_inner.addWidget(apply_w)

        self._settings_status = QLabel('')
        self._settings_status.setFont(QFont(_FONT_SANS, 10))
        self._settings_status.setWordWrap(True)
        net_inner.addWidget(self._settings_status)

        # Wrong-protocol banner
        self._mismatch_frame = QFrame()
        self._mismatch_frame.setStyleSheet(
            f'QFrame {{ background-color: {self.BG}; border: 1px solid {self.ORANGE};'
            f' border-radius: 8px; }}')
        mm_l = QHBoxLayout(self._mismatch_frame)
        mm_l.setContentsMargins(12, 8, 12, 8)
        self._mismatch_lbl = QLabel('')
        self._mismatch_lbl.setWordWrap(True)
        self._mismatch_lbl.setFont(QFont(_FONT_SANS, 10))
        self._mismatch_lbl.setStyleSheet(f'color: {self.ORANGE}; background: transparent; border: none;')
        mm_l.addWidget(self._mismatch_lbl, stretch=1)
        self._mismatch_btn = QPushButton('Switch')
        self._mismatch_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.ORANGE}; color: #000000;
                border: none; border-radius: 6px; padding: 5px 14px;
                font-family: {_FONT_SANS}; font-size: 12px; font-weight: bold;
            }}
        """)
        self._mismatch_btn.clicked.connect(self._on_mismatch_switch)
        mm_l.addWidget(self._mismatch_btn)
        self._mismatch_frame.setVisible(False)
        self._mismatch = None
        net_inner.addWidget(self._mismatch_frame)

        net_layout.addWidget(net_frame)

        # ── Detected senders card ─────────────────────────────────────
        snd_frame = QFrame()
        snd_frame.setObjectName('card')
        snd_inner = QVBoxLayout(snd_frame)
        snd_inner.setContentsMargins(16, 12, 16, 14)
        snd_inner.setSpacing(8)
        snd_inner.addWidget(_title('DETECTED SENDERS'))
        snd_inner.addWidget(_small('Everything arriving on the listening ports — protocol identified '
                                   'from the packet header. Rate is samples per second.'))
        tbl = QTableWidget(0, 7)
        tbl.setHorizontalHeaderLabels(['Sender', 'Port', 'Protocol', 'Device', 'Rate', 'Last seen', 'Status'])
        hh = tbl.horizontalHeader()
        for col, w in enumerate([150, 60, 100, 0, 80, 80, 130]):
            if col == 3:
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            else:
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                tbl.setColumnWidth(col, w)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.setMinimumHeight(130)
        self._senders_table = tbl
        self._senders_font = QFont(_FONT_MONO, 10)
        snd_inner.addWidget(tbl)
        net_layout.addWidget(snd_frame)

        self._sender_combo_keys = {}
        self._refresh_network_rows()
        self._update_settings_status()

    def _refresh_network_rows(self):
        mode = self.forwarder.input_mode
        self._freed_port_row.setVisible(mode in ('freed', 'both', 'auto'))
        self._oti_port_row.setVisible(mode in ('oti', 'both'))
        self._freed_port_lbl.setText('Port (FreeD + OTI)' if mode == 'auto' else 'FreeD port')
        self._mode_hint.setText({
            'freed': 'FreeD D1 only',
            'oti':   'OpenTrackIO only',
            'both':  'Each protocol on its own port — pick the displayed/output source in the header',
            'auto':  'One port — follows whichever protocol is arriving',
        }[mode])
        self._sender_combo_keys = {}        # force sender combos to rebuild
        self._update_network_ui()

    def _update_settings_status(self):
        if not hasattr(self, '_settings_status'):
            return
        if self._listen_errors:
            port, err = next(iter(self._listen_errors.items()))
            self._settings_status.setText(f'● Cannot listen on port {port}: {err}')
            self._settings_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            return
        parts = [f"{port} ({' + '.join(PROTO_NAMES[p] for p in sorted(acc))})"
                 for port, acc in sorted(self._listen_plan().items())]
        self._settings_status.setText('● Listening on ' + ',  '.join(parts))
        self._settings_status.setStyleSheet(f'color: {self.GREEN}; background: transparent;')

    def _on_input_mode_changed(self, idx: int):
        mode = self._mode_combo.itemData(idx)
        if mode not in FreeDForwarder.INPUT_MODES:
            return
        self.forwarder.input_mode = mode
        self._active_source = self._resolve_active_source()
        self.forwarder.save_config()
        self._restart_listeners()
        self._refresh_network_rows()

    def _on_apply_ports(self):
        f = self.forwarder
        freed_port, oti_port = self._freed_port_spin.value(), self._oti_port_spin.value()
        if (freed_port, oti_port) == (f.listen_port, f.oti_listen_port) and not self._listen_errors:
            return
        self._settings_status.setText('● Restarting…')
        self._settings_status.setStyleSheet(f'color: {self.YELLOW}; background: transparent;')
        QApplication.processEvents()
        f.listen_port, f.oti_listen_port = freed_port, oti_port
        f.save_config()
        self._restart_listeners()

    def _on_sender_combo_changed(self, proto: str):
        combo = self._freed_sender_combo if proto == PROTO_FREED else self._oti_sender_combo
        ip = combo.currentData() or ''
        f = self.forwarder
        if f.input_mode == 'auto':
            f.freed_sender_ip = f.oti_sender_ip = ip     # one port, one filter
        elif proto == PROTO_FREED:
            f.freed_sender_ip = ip
        else:
            f.oti_sender_ip = ip
        f.save_config()
        flt = self._sender_filter()
        for lst in self.listeners.values():
            lst.ip_filter = flt
        self._sender_combo_keys = {}
        self._update_network_ui()

    def _on_mismatch_switch(self):
        if not self._mismatch:
            return
        proto, port = self._mismatch
        f = self.forwarder
        if f.input_mode in ('freed', 'oti'):
            f.input_mode = proto
        if proto == PROTO_OTI:
            f.oti_listen_port = port
        else:
            f.listen_port = port
        self._active_source = self._resolve_active_source()
        f.save_config()
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(FreeDForwarder.INPUT_MODES.index(f.input_mode))
        self._mode_combo.blockSignals(False)
        self._freed_port_spin.setValue(f.listen_port)
        self._oti_port_spin.setValue(f.oti_listen_port)
        self._mismatch = None
        self._mismatch_frame.setVisible(False)
        self._restart_listeners()
        self._refresh_network_rows()

    def _sync_sender_combo(self, combo: QComboBox, protos: set, current_ip: str, senders: list):
        ips = sorted({s['addr'][0] for s in senders if s['proto'] in protos})
        key = (tuple(ips), current_ip)
        if self._sender_combo_keys.get(id(combo)) == key:
            return
        self._sender_combo_keys[id(combo)] = key
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('Any sender', '')
        for ip in ips:
            combo.addItem(ip, ip)
        if current_ip and current_ip not in ips:
            combo.addItem(f'{current_ip}  (not seen)', current_ip)
        for i in range(combo.count()):
            if combo.itemData(i) == current_ip:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _update_network_ui(self):
        if not hasattr(self, '_senders_table'):
            return
        now  = time.perf_counter()
        f    = self.forwarder
        plan = self._listen_plan()
        flt  = self._sender_filter()
        senders = []
        for lst in self.listeners.values():
            senders.extend(lst.senders(now))
        senders.sort(key=lambda s: (s['port'], s['addr']))

        # Sender pickers
        if f.input_mode == 'auto':
            self._sync_sender_combo(self._freed_sender_combo, {PROTO_FREED, PROTO_OTI},
                                    f.freed_sender_ip, senders)
        else:
            self._sync_sender_combo(self._freed_sender_combo, {PROTO_FREED}, f.freed_sender_ip, senders)
        self._sync_sender_combo(self._oti_sender_combo, {PROTO_OTI}, f.oti_sender_ip, senders)

        # Table
        tbl = self._senders_table
        tbl.setRowCount(len(senders))
        for row, s in enumerate(senders):
            proto = s['proto']
            acc   = plan.get(s['port'], set())
            if proto is None:
                status, color = 'Unknown data', self.DIM
            elif proto not in acc:
                status, color = 'Wrong port', self.ORANGE
            elif flt.get(proto) and flt[proto] != s['addr'][0]:
                status, color = 'Ignored (filter)', self.DIM
            elif s['age'] < 2.0:
                status, color = 'Receiving', self.GREEN
            else:
                status, color = 'Idle', self.DIM
            cells = [
                f"{s['addr'][0]}:{s['addr'][1]}",
                str(s['port']),
                PROTO_NAMES.get(proto, '?'),
                self._sender_labels.get(s['addr'], ''),
                f"{s['rate']:.2f}" if s['rate'] else '---',
                f"{s['age']:.1f} s ago" if s['age'] >= 1.0 else 'now',
                status,
            ]
            for col, text in enumerate(cells):
                item = tbl.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    item.setFont(self._senders_font)
                    tbl.setItem(row, col, item)
                item.setText(text)
                item.setForeground(QColor(color if col == 6 else self.FG))

        # Wrong-protocol banner (from any listener, last 3 s)
        mm = None
        for port, lst in self.listeners.items():
            if lst.mismatch and now - lst.mismatch[2] < 3.0:
                mm = (lst.mismatch[0], port, lst.mismatch[1])
                break
        if mm is None:
            self._mismatch = None
            self._mismatch_frame.setVisible(False)
        else:
            proto, port, addr = mm
            other = PROTO_NAMES[proto]
            expected = ' + '.join(PROTO_NAMES[p] for p in sorted(plan.get(port, set())))
            self._mismatch = (proto, port)
            self._mismatch_lbl.setText(
                f'{other} is arriving on port {port} from {addr[0]}:{addr[1]}, '
                f'but this port is set to {expected}.')
            self._mismatch_btn.setText(f'Use {other} on {port}')
            self._mismatch_frame.setVisible(True)

    # ------------------------------------------------------------------
    # OpenTrackIO tab
    # ------------------------------------------------------------------

    _OTI_CARDS = [
        ('DEVICE',     ['Camera', 'Camera S/N', 'Sensor', 'Exposure', 'Tracker',
                        'Tracker S/N', 'Tracker status', 'Lens', 'Lens S/N']),
        ('TRANSFORM',  ['Transform', 'X', 'Y', 'Z', 'Pan', 'Tilt', 'Roll', 'Chain']),
        ('LENS',       ['Focal length', 'Focus distance', 'F-stop', 'T-stop', 'Entrance pupil',
                        'Encoders F/I/Z', 'Raw enc F/I/Z', 'Projection offset', 'Nominal FL']),
        ('DISTORTION', ['Model', 'Radial', 'Tangential', 'Overscan', 'Distortion offset', 'Other lens']),
        ('TIMING',     ['Timecode', 'TC rate', 'Sample rate', 'Mode', 'Sequence',
                        'Genlock', 'Sync source', 'Sync freq']),
        ('STREAM',     ['Sender', 'Samples', 'Segments', 'Last sample', 'Checksum errors',
                        'Lost samples', 'Incomplete', 'Decode errors', 'Protocol', 'Source']),
    ]
    _OTI_CARD_COLORS = {'DEVICE': 'FG', 'TRANSFORM': 'CYAN', 'LENS': 'YELLOW',
                        'DISTORTION': 'YELLOW', 'TIMING': 'ORANGE', 'STREAM': 'FG'}
    _OTI_KNOWN_LENS = {'pinholeFocalLength', 'focusDistance', 'fStop', 'tStop', 'entrancePupilOffset',
                       'encoders', 'rawEncoders', 'projectionOffset', 'distortion',
                       'distortionOverscan', 'distortionOffset'}

    def _build_oti_tab(self, parent: QWidget):
        outer = QVBoxLayout(parent)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        grid = QGridLayout(body)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self._oti_hdr = QLabel('Waiting for OpenTrackIO…')
        self._oti_hdr.setFont(QFont(_FONT_SANS, 11, QFont.Weight.Bold))
        self._oti_hdr.setStyleSheet(f'color: {self.DIM}; background: transparent;')
        grid.addWidget(self._oti_hdr, 0, 0, 1, 2)

        self._oti_lbl = {}
        for i, (title, keys) in enumerate(self._OTI_CARDS):
            w, form = self._card(title)
            color = getattr(self, self._OTI_CARD_COLORS[title])
            for key in keys:
                v = self._val(color, size=10)
                v.setWordWrap(True)
                v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(self._key(key), v)
                self._oti_lbl[key] = v
            grid.addWidget(w, 1 + i // 2, i % 2)

        raw_w, raw_form = self._card('RAW JSON  (last sample)')
        self._oti_json = QTextEdit()
        self._oti_json.setReadOnly(True)
        self._oti_json.setMinimumHeight(260)
        self._oti_json.setFont(QFont(_FONT_MONO, 9))
        self._oti_json.setStyleSheet(
            f'QTextEdit {{ background-color: {self.BG}; color: #aaaaaa;'
            f' border: 1px solid {self.BORDER}; border-radius: 6px; }}')
        raw_form.addRow(self._oti_json)
        grid.addWidget(raw_w, 1 + (len(self._OTI_CARDS) + 1) // 2, 0, 1, 2)

    @staticmethod
    def _fr(r) -> str:
        """Format an OpenTrackIO rational {num, denom}."""
        if isinstance(r, dict) and r.get('denom'):
            return f"{r['num']}/{r['denom']}  ({r['num'] / r['denom']:.3f})"
        return '---' if r is None else str(r)

    @staticmethod
    def _fn(v, fmt: str = '{:.4f}', unit: str = '') -> str:
        if isinstance(v, bool) or v is None:
            return '---' if v is None else str(v)
        if isinstance(v, (int, float)):
            return fmt.format(v) + unit
        return str(v)

    @staticmethod
    def _join(*parts) -> str:
        return '  ·  '.join(str(p) for p in parts if p not in (None, '')) or '---'

    def _update_oti_tab(self, full: bool = False):
        s    = self.oti_state
        data = s.latest_data
        f    = self.forwarder
        if data is None:
            if f.input_mode == 'freed':
                self._oti_hdr.setText('OpenTrackIO input is off — choose OpenTrackIO, Both or '
                                      'Auto-detect in Settings › Network')
            else:
                port = f.listen_port if f.input_mode == 'auto' else f.oti_listen_port
                self._oti_hdr.setText(f'Waiting for OpenTrackIO on port {port}…')
            return

        smp  = data['oti']
        addr = s.latest_addr
        now  = time.perf_counter()
        stale = s._last_packet_time is not None and now - s._last_packet_time > 2.0
        if stale:
            self._oti_hdr.setText(f'● TIMEOUT — last sample from {oti_device_label(smp)}')
            self._oti_hdr.setStyleSheet(f'color: {self.RED}; background: transparent;')
        else:
            where = f'  @  {addr[0]}:{addr[1]}' if addr else ''
            shown = '' if self._active_source == PROTO_OTI else '   (not the active source)'
            self._oti_hdr.setText(f'● LIVE  {oti_device_label(smp)}{where}{shown}')
            self._oti_hdr.setStyleSheet(f'color: {self.GREEN}; background: transparent;')

        fn, fr, jn = self._fn, self._fr, self._join
        st   = smp.get('static') or {}
        cam  = st.get('camera') or {}
        strk = st.get('tracker') or {}
        slen = st.get('lens') or {}
        trk  = smp.get('tracker') or {}
        lens = smp.get('lens') or {}
        tm   = smp.get('timing') or {}
        sync = tm.get('synchronization') or {}
        meta = smp.get('_meta') or {}
        t    = oti_camera_transform(smp)
        tr, rot = t.get('translation') or {}, t.get('rotation') or {}

        dims = cam.get('activeSensorPhysicalDimensions') or {}
        res  = cam.get('activeSensorResolution') or {}
        sensor = jn(f"{fn(dims.get('width'), '{:.3f}')} × {fn(dims.get('height'), '{:.3f}')} mm" if dims else '',
                    f"{res.get('width')} × {res.get('height')} px" if res else '')

        dist = lens.get('distortion')
        if isinstance(dist, dict):
            dist = [dist]
        d0 = dist[0] if dist else {}
        model = d0.get('model', '---' if not dist else 'default')
        if dist and len(dist) > 1:
            model += f'  (+{len(dist) - 1} more)'

        def _vec(d, keys, fmt='{:.4f}'):
            d = d or {}
            return ' / '.join(fn(d.get(k), fmt) for k in keys) if d else '---'

        def _xy(d, unit=''):
            return f"x {fn(d.get('x'))}  y {fn(d.get('y'))}{unit}" if d else '---'

        tc = tm.get('timecode') or {}
        if tc:
            sep = ';' if tc.get('dropFrame') else ':'
            tc_str = (f"{tc.get('hours', 0):02d}:{tc.get('minutes', 0):02d}:"
                      f"{tc.get('seconds', 0):02d}{sep}{tc.get('frames', 0):02d}")
            if tc.get('subFrame'):
                tc_str += f'.{tc["subFrame"]}'
        else:
            tc_str = '---'

        p = self.oti_parser
        proto = smp.get('protocol') or {}
        ver = proto.get('version') or []
        other_lens = sorted(set(lens) - self._OTI_KNOWN_LENS)

        vals = {
            'Camera':          jn(f"{cam.get('make', '')} {cam.get('model', '')}".strip(), cam.get('label')),
            'Camera S/N':      jn(cam.get('serialNumber'), 'fw ' + cam['firmwareVersion'] if cam.get('firmwareVersion') else ''),
            'Sensor':          sensor,
            'Exposure':        jn('ISO ' + str(cam['isoSpeed']) if 'isoSpeed' in cam else '',
                                  f"{cam['shutterAngle']}°" if 'shutterAngle' in cam else '',
                                  fr(cam['captureFrameRate']) + ' fps' if 'captureFrameRate' in cam else ''),
            'Tracker':         f"{strk.get('make', '')} {strk.get('model', '')}".strip() or '---',
            'Tracker S/N':     jn(strk.get('serialNumber'), 'fw ' + strk['firmwareVersion'] if strk.get('firmwareVersion') else ''),
            'Tracker status':  jn(trk.get('status'),
                                  ('REC' if trk.get('recording') else 'not recording') if 'recording' in trk else '',
                                  'slate ' + str(trk['slate']) if trk.get('slate') else ''),
            'Lens':            f"{slen.get('make', '')} {slen.get('model', '')}".strip() or '---',
            'Lens S/N':        jn(slen.get('serialNumber'), 'fw ' + slen['firmwareVersion'] if slen.get('firmwareVersion') else ''),

            'Transform':       str(t.get('id', '---')),
            'X':               fn(tr.get('x'), '{:+.5f}', ' m'),
            'Y':               fn(tr.get('y'), '{:+.5f}', ' m'),
            'Z':               fn(tr.get('z'), '{:+.5f}', ' m'),
            'Pan':             fn(rot.get('pan'),  '{:+.4f}', '°'),
            'Tilt':            fn(rot.get('tilt'), '{:+.4f}', '°'),
            'Roll':            fn(rot.get('roll'), '{:+.4f}', '°'),
            'Chain':           ' → '.join(str(x.get('id', '?')) for x in (smp.get('transforms') or [])) or '---',

            'Focal length':    fn(lens.get('pinholeFocalLength'), '{:.3f}', ' mm'),
            'Focus distance':  fn(lens.get('focusDistance'), '{:.3f}', ' m'),
            'F-stop':          fn(lens.get('fStop'), '{:.2f}'),
            'T-stop':          fn(lens.get('tStop'), '{:.2f}'),
            'Entrance pupil':  fn(lens.get('entrancePupilOffset'), '{:.4f}', ' m'),
            'Encoders F/I/Z':  _vec(lens.get('encoders'), ('focus', 'iris', 'zoom')),
            'Raw enc F/I/Z':   _vec(lens.get('rawEncoders'), ('focus', 'iris', 'zoom'), '{}'),
            'Projection offset': _xy(lens.get('projectionOffset'), ' mm'),
            'Nominal FL':      fn(slen.get('nominalFocalLength'), '{:.1f}', ' mm'),

            'Model':           model,
            'Radial':          ', '.join(f'{k:.6g}' for k in d0.get('radial', [])) or '---',
            'Tangential':      ', '.join(f'{k:.6g}' for k in d0.get('tangential', [])) or '---',
            'Overscan':        fn(lens.get('distortionOverscan', d0.get('overscan')), '{:.4f}'),
            'Distortion offset': _xy(lens.get('distortionOffset'), ' mm'),
            'Other lens':      ', '.join(other_lens) or '---',

            'Timecode':        tc_str,
            'TC rate':         fr(tc.get('frameRate')) if tc else '---',
            'Sample rate':     fr(tm.get('sampleRate')),
            'Mode':            str(tm.get('mode', '---')),
            'Sequence':        jn(tm.get('sequenceNumber'), f"pkt #{meta.get('seq')}" if 'seq' in meta else ''),
            'Genlock':         ('LOCKED' if sync.get('locked') else 'UNLOCKED') if sync else '---',
            'Sync source':     jn(sync.get('source'), ('present' if sync.get('present') else 'absent') if 'present' in sync else ''),
            'Sync freq':       fr(sync.get('frequency')) if sync.get('frequency') else '---',

            'Sender':          f'{addr[0]}:{addr[1]}' if addr else '---',
            'Samples':         f'{p.sample_count:,}' + (f'  ({s.packet_fps:.2f}/s)' if s.packet_fps else ''),
            'Segments':        f'{p.segment_count:,}',
            'Last sample':     jn(f"{meta.get('payload_size', 0):,} bytes", f"{meta.get('segments', 1)} seg",
                                  meta.get('encoding')),
            'Checksum errors': f'{p.checksum_errors:,}',
            'Lost samples':    f'{p.seq_gaps:,}',
            'Incomplete':      f'{p.incomplete_count:,}',
            'Decode errors':   jn(f'{p.decode_errors:,}', p.last_error if p.decode_errors else ''),
            'Protocol':        jn(proto.get('name'), 'v' + '.'.join(str(v) for v in ver) if ver else ''),
            'Source':          jn(smp.get('sourceId'), f"#{smp['sourceNumber']}" if 'sourceNumber' in smp else ''),
        }
        for key, text in vals.items():
            self._oti_lbl[key].setText(text)

        gl = self._oti_lbl['Genlock']
        gl.setStyleSheet(f"color: {self.GREEN if sync.get('locked') else self.RED}; background: transparent;")
        for key in ('Checksum errors', 'Lost samples', 'Incomplete', 'Decode errors'):
            bad = not vals[key].startswith('0')
            self._oti_lbl[key].setStyleSheet(
                f'color: {self.ORANGE if bad else self.FG}; background: transparent;')

        if full:
            text = json.dumps({k: v for k, v in smp.items() if k != '_meta'}, indent=2)
            if text != self._oti_json.toPlainText():
                bar = self._oti_json.verticalScrollBar()
                pos = bar.value()
                self._oti_json.setPlainText(text)
                bar.setValue(pos)

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
        self._tick = getattr(self, '_tick', 0) + 1
        try:
            if self._tabs.currentIndex() == self._oti_tab_index:
                self._update_oti_tab(full=(self._tick % 5 == 0))
            if self._tick % 5 == 0:
                self._update_network_ui()
        except Exception as e:
            print(f'[UI] {e}', flush=True)

    def _update_fwd_ui(self):
        try:
            self._fwd_count_lbl.setText(
                f'Forwarded: {self.forwarder.packets_forwarded:,} pkts')
            self._tc_preview_lbl.setText(
                self.forwarder.current_tc_str(self.ltc_reader))
        except Exception:
            pass

    def _update(self):
        self._update_source_badge()

        dead = [(p, l.last_error) for p, l in self.listeners.items() if not l.is_alive()]
        errs = list(self._listen_errors.items()) + dead
        if errs:
            port, err = errs[0]
            self.lbl_status.setText(f'● RX DEAD :{port}  {(err or "unknown error")[:40]}')
            self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            return

        r    = self._view_state()
        data = r.latest_data
        addr = r.latest_addr

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
            self.lbl_status.setText(f'● LISTENING :{self._ports_str()}  [{self._cached_ip_str}]')
            self.lbl_status.setStyleSheet(f'color: {self.CYAN}; background: transparent;')
            return

        now = time.perf_counter()
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

        oti = data.get('oti')
        if oti is not None:
            meta  = oti.get('_meta', {})
            ver   = (oti.get('protocol') or {}).get('version') or []
            vstr  = 'v' + '.'.join(str(v) for v in ver) if ver else ''
            self.lbl_proto.setText(f"OpenTrackIO {vstr}  ({meta.get('encoding', '?')})")
            self.lbl_rawsize.setText(
                f"{meta.get('payload_size', 0):,} bytes  ·  {meta.get('segments', 1)} segment(s)")
            hdr = meta.get('header', b'')
            self.lbl_hex1.setText('HDR ' + ' '.join(f'{b:02X}' for b in hdr))
            self.lbl_hex2.setText('D1  ' + ' '.join(f'{b:02X}' for b in data['raw_bytes'][:29]))
            sync = (oti.get('timing') or {}).get('synchronization') or {}
            if sync:
                present = 'present' if sync.get('present') else 'absent'
                self.lbl_gl_ref.setText(f"{sync.get('source', '?')}  ({present})")

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
        r       = self._view_state()
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

        # Health assessment based on RFC jitter and genlock state
        is_locked = len(set(r._gl_phase_history)) > 1

        if is_locked:
            # Genlocked: frame alignment is handled by sync signal — timing jitter is far less critical
            if rfc < 5.0:
                health_color = self.GREEN
                health_title = 'GOOD  ·  GENLOCKED'
                health_sub   = f'RFC jitter {rfc:.2f} ms — frame-aligned via genlock, timing offset is compensated'
            elif rfc < 15.0:
                health_color = self.YELLOW
                health_title = 'ACCEPTABLE  ·  GENLOCKED'
                health_sub   = f'RFC jitter {rfc:.2f} ms — genlocked but high transport jitter, check network path'
            else:
                health_color = self.ORANGE
                health_title = 'MARGINAL  ·  GENLOCKED'
                health_sub   = f'RFC jitter {rfc:.2f} ms — genlocked but excessive jitter may cause missed frames'
        else:
            # Free-running: strict thresholds apply
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
                health_title = 'PROBLEMATIC  ·  NOT GENLOCKED'
                health_sub   = f'RFC jitter {rfc:.2f} ms — visible judder likely on LED wall, fix network or add genlock'

        self._jitter_health_dot.setStyleSheet(f'color: {health_color}; background: transparent;')
        self._jitter_health_lbl.setText(health_title)
        self._jitter_health_lbl.setStyleSheet(f'color: {health_color}; background: transparent;')
        self._jitter_health_sub.setText(health_sub)

        # Color RFC JITTER stat card dynamically
        self._jitter_stat_labels['RFC JITTER'].setStyleSheet(
            f'color: {health_color}; background: transparent;')

        # ── Position noise ─────────────────────────────────────────────
        pos_scale_mm = r.position_scale   # 1/64 → mm
        for axis, hist in (('X', r._x_history), ('Y', r._y_history), ('Z', r._z_history)):
            lbl = self._noise_pos_labels[axis]
            if len(hist) < 10:
                lbl.setText('…')
                lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
                continue
            std_mm = float(np.std(np.array(hist, dtype=np.float64))) * pos_scale_mm
            if std_mm < 0.1:
                c = self.GREEN
            elif std_mm < 0.5:
                c = self.YELLOW
            else:
                c = self.RED
            lbl.setText(f'{std_mm:.5f}')
            lbl.setStyleSheet(f'color: {c}; background: transparent;')

        # ── Rotation noise ─────────────────────────────────────────────
        rot_scale = r.rotation_scale   # 1/32768 → degrees
        for axis, hist in (('Pan', r._pan_history), ('Tilt', r._tilt_history), ('Roll', r._roll_history)):
            lbl = self._noise_rot_labels[axis]
            if len(hist) < 10:
                lbl.setText('…')
                lbl.setStyleSheet(f'color: {self.DIM}; background: transparent;')
                continue
            std_deg = float(np.std(np.array(hist, dtype=np.float64))) * rot_scale
            if std_deg < 0.01:
                c = self.GREEN
            elif std_deg < 0.05:
                c = self.YELLOW
            else:
                c = self.RED
            lbl.setText(f'{std_deg:.6f}')
            lbl.setStyleSheet(f'color: {c}; background: transparent;')

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._timer.stop()
        self.ltc_reader.stop()
        self.forwarder.save_config()
        if sys.platform == 'win32':
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        self.forwarder.close()
        self.oti_sender.close()
        self._stop_listeners()
        event.accept()



def main_gui():
    """GUI entry point — launches Apple-dark PyQt6 dashboard"""
    app = QApplication(sys.argv)
    app.setStyle('Fusion')   # ensures custom stylesheets render correctly on Windows
    app.setApplicationName('FreeD Dashboard')
    window = FreeDDashboard()
    # Centre the window on the primary screen to avoid off-screen placement
    # on multi-monitor or high-DPI setups.
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        window.move(
            available.x() + (available.width()  - window.width())  // 2,
            available.y() + (available.height() - window.height()) // 2,
        )
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
