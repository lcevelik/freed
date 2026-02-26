#!/usr/bin/env python3
"""
FreeD Protocol Reader
Reads and parses FreeD camera tracking data from UDP socket

Version : v1.0
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

__version__   = 'v1.0'
__author__    = 'Libor Cevelik'
__copyright__ = 'Copyright (c) 2026 Libor Cevelik'

import os
import socket
import struct
import sys
import threading
import time
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont, QColor
from collections import deque
from datetime import datetime

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
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, TypeError):
        # Windowed mode — no console attached; write to devnull instead of None
        _devnull = open(os.devnull, 'w')
        sys.stdout = _devnull
        sys.stderr = _devnull


class FreeDParser:
    """Parser for FreeD (D1) protocol data"""

    FREED_PACKET_SIZE = 29
    FREED_MESSAGE_TYPE = 0xD1

    def __init__(self, debug=False, ignore_checksum=False):
        self.packet_count = 0
        self.error_count = 0
        self.debug = debug
        self.ignore_checksum = ignore_checksum

    def parse_24bit_int(self, data: bytes) -> int:
        """Convert 3 bytes to signed 24-bit integer"""
        value = int.from_bytes(data, byteorder='big', signed=False)
        if value & 0x800000:
            value -= 0x1000000
        return value

    def calculate_checksum(self, data: bytes) -> int:
        """Calculate XOR checksum of all bytes"""
        checksum = 0
        for byte in data:
            checksum ^= byte
        return checksum

    def parse(self, data: bytes) -> dict:
        """
        Parse FreeD protocol packet
        Returns dict with camera tracking data or None if invalid
        """
        error_reason = None
        checksum_valid = True
        extra_bytes = None

        # Check packet size - allow larger packets but warn
        if len(data) < self.FREED_PACKET_SIZE:
            error_reason = f"Packet too small: {len(data)} bytes (expected {self.FREED_PACKET_SIZE})"
            if self.debug:
                print(f"  ERROR: {error_reason}")
            return None
        elif len(data) > self.FREED_PACKET_SIZE:
            # Packet is larger than expected - capture extra bytes for analysis
            extra_bytes = data[self.FREED_PACKET_SIZE:]
            if self.debug:
                print(f"  WARNING: Packet larger than expected: {len(data)} bytes (expected {self.FREED_PACKET_SIZE})")
                print(f"  Extra {len(extra_bytes)} bytes detected: {extra_bytes.hex(' ').upper()}")

        # Verify message type
        if data[0] != self.FREED_MESSAGE_TYPE:
            error_reason = f"Invalid message type: 0x{data[0]:02X} (expected 0x{self.FREED_MESSAGE_TYPE:02X})"
            if self.debug:
                print(f"  ERROR: {error_reason}")
            return None

        # Verify checksum
        calculated_checksum = self.calculate_checksum(data[:-1])
        packet_checksum = data[-1]

        if calculated_checksum != packet_checksum:
            checksum_valid = False
            if not self.ignore_checksum:
                self.error_count += 1
                if self.debug:
                    print(f"  WARNING: Checksum mismatch: 0x{packet_checksum:02X} (expected 0x{calculated_checksum:02X}) - Parsing anyway")
            # Continue parsing despite checksum error (silently if ignore_checksum is True)

        # Parse data
        camera_id = data[1]
        pan = self.parse_24bit_int(data[2:5])
        tilt = self.parse_24bit_int(data[5:8])
        roll = self.parse_24bit_int(data[8:11])
        x = self.parse_24bit_int(data[11:14])
        y = self.parse_24bit_int(data[14:17])
        z = self.parse_24bit_int(data[17:20])
        zoom = self.parse_24bit_int(data[20:23])
        focus = self.parse_24bit_int(data[23:26])
        spare = struct.unpack('>H', data[26:28])[0]
        spare_bytes = data[26:28]

        self.packet_count += 1

        return {
            'camera_id': camera_id,
            'pan': pan,
            'tilt': tilt,
            'roll': roll,
            'position': {'x': x, 'y': y, 'z': z},
            'zoom': zoom,
            'focus': focus,
            'spare': spare,
            'spare_bytes': spare_bytes,
            'checksum_valid': checksum_valid,
            'checksum_expected': calculated_checksum,
            'checksum_actual': packet_checksum,
            'timestamp': datetime.now().isoformat(),
            'extra_bytes': extra_bytes,
            'packet_size': len(data),
            'message_type': data[0],
            'raw_bytes': bytes(data)
        }


class FreeDReceiver:
    """UDP receiver for FreeD protocol data"""

    def __init__(self, host: str = '0.0.0.0', port: int = 45000, debug: bool = False, step_by_step: bool = False, delay: float = 0.0, ignore_checksum: bool = False, timecode_fps: float = None, convert_units: bool = False, clear_screen: bool = False):
        self.host = host
        self.port = port
        self.socket = None
        self.parser = FreeDParser(debug=debug, ignore_checksum=ignore_checksum)
        self.running = False
        self.debug = debug
        self._last_error = None   # set if receive_loop exits due to an exception
        self.step_by_step = step_by_step
        self.delay = delay
        self.ignore_checksum = ignore_checksum
        self.timecode_fps = timecode_fps
        self.convert_units = convert_units
        self.clear_screen = clear_screen

        # Timecode tracking for analysis
        self.last_spare_value = None
        self.spare_increment_count = 0
        self.spare_same_count = 0

        # Conversion scale factors (based on common FreeD implementations)
        # Position: most systems use raw / 64.0 = millimeters
        # Rotation: raw / 32768 = degrees (verified with actual tracking data)
        self.rotation_scale = 1.0 / 32768.0  # raw / 32768 = degrees
        self.position_scale = 1.0 / 64.0     # raw / 64.0 = millimeters

        # Zoom: Linear encoding - raw = focal_length × 1000
        # Fujinon Premista 28-100mm on your specific camera/tracking setup
        self.zoom_calibration = [
            (28000, 28.0),    # 28mm wide angle
            (35000, 35.0),    # 35mm
            (50000, 50.0),    # 50mm
            (70000, 70.0),    # 70mm
            (100000, 100.0)   # 100mm telephoto
        ]

        # Focus: Linear encoding - raw = distance × 1000
        # Fujinon Premista 28-100mm on your specific camera/tracking setup
        self.focus_calibration = [
            (800, 0.8),       # 0.8m MOD (Minimum Object Distance)
            (892, 0.892),     # 0.892m
            (1299, 1.299),    # 1.299m
            (4170, 4.170),    # 4.170m
            (629000, 629.0)   # 629m (infinity/far focus)
        ]

    def interpolate_zoom(self, raw_value: float) -> float:
        """
        Interpolate focal length using measured calibration points
        Uses piecewise linear interpolation between calibration points
        """
        # Handle edge cases
        if raw_value <= self.zoom_calibration[0][0]:
            return self.zoom_calibration[0][1]
        if raw_value >= self.zoom_calibration[-1][0]:
            return self.zoom_calibration[-1][1]

        # Find the two calibration points to interpolate between
        for i in range(len(self.zoom_calibration) - 1):
            raw_lower, zoom_lower = self.zoom_calibration[i]
            raw_upper, zoom_upper = self.zoom_calibration[i + 1]

            if raw_lower <= raw_value <= raw_upper:
                # Linear interpolation between calibration points
                t = (raw_value - raw_lower) / (raw_upper - raw_lower)
                return zoom_lower + t * (zoom_upper - zoom_lower)

        # Fallback (shouldn't reach here)
        return self.zoom_calibration[0][1]

    def interpolate_focus(self, raw_value: float) -> float:
        """
        Interpolate focus distance using measured calibration points
        Uses piecewise linear interpolation between calibration points
        """
        # Handle edge cases
        if raw_value <= self.focus_calibration[0][0]:
            return self.focus_calibration[0][1]
        if raw_value >= self.focus_calibration[-1][0]:
            return self.focus_calibration[-1][1]

        # Find the two calibration points to interpolate between
        for i in range(len(self.focus_calibration) - 1):
            raw_lower, focus_lower = self.focus_calibration[i]
            raw_upper, focus_upper = self.focus_calibration[i + 1]

            if raw_lower <= raw_value <= raw_upper:
                # Linear interpolation between calibration points
                t = (raw_value - raw_lower) / (raw_upper - raw_lower)
                return focus_lower + t * (focus_upper - focus_lower)

        # Fallback (shouldn't reach here)
        return self.focus_calibration[0][1]

    def parse_timecode(self, spare_value: int, fps: float) -> str:
        """
        Parse timecode from spare bytes value
        Assumes spare_value is total frame count
        Returns timecode in HH:MM:SS:FF format
        """
        if fps is None or fps <= 0:
            return None

        total_frames = spare_value
        frames_per_second = int(fps)

        # Calculate timecode components
        frames = total_frames % frames_per_second
        total_seconds = total_frames // frames_per_second
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    def start(self):
        """Start listening for FreeD data"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            # Enable address reuse - allows multiple programs to bind to the same port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Enable port reuse on systems that support it (Unix/Linux/macOS)
            # On Windows, SO_REUSEADDR is sufficient
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                # SO_REUSEPORT not available on Windows or some systems
                pass

            # Enable broadcast reception if FreeD data is sent as broadcast
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass

            self.socket.bind((self.host, self.port))

            print(f"FreeD Receiver started")
            print(f"Listening on {self.host}:{self.port}")
            print(f"Waiting for FreeD packets...")
            print("-" * 80)

            self.running = True
            self.receive_loop()

        except Exception as e:
            print(f"Error starting receiver: {e}")
            sys.exit(1)

    def receive_loop(self):
        """Main receive loop"""
        try:
            while self.running:
                try:
                    data, addr = self.socket.recvfrom(1024)
                except socket.timeout:
                    continue  # no data yet — keep waiting, don't kill the thread

                # Show raw packet in debug mode
                if self.debug:
                    print(f"\n{'='*80}")
                    print(f"Packet #{self.parser.packet_count + self.parser.error_count + 1} from {addr[0]}:{addr[1]}")
                    print(f"Size: {len(data)} bytes")
                    print(f"Raw hex: {data.hex(' ')}")
                    print(f"Raw bytes: {' '.join(f'{b:02X}' for b in data)}")

                # Parse FreeD data
                parsed_data = self.parser.parse(data)

                if parsed_data:
                    self.display_data(parsed_data, addr)
                else:
                    if not self.debug:
                        print(f"\nInvalid packet from {addr[0]}:{addr[1]} - Size: {len(data)} bytes - Hex: {data[:10].hex(' ')}...")

                # Add delay if specified
                if self.delay > 0:
                    time.sleep(self.delay)

                # Wait for user input in step-by-step mode
                if self.step_by_step:
                    try:
                        input("\nPress Enter for next packet (or Ctrl+C to quit)...")
                    except KeyboardInterrupt:
                        raise

        except KeyboardInterrupt:
            print("\n\nShutting down...")
            self.stop()
        except Exception as e:
            self._last_error = str(e)
            print(f"Error in receive loop: {e}")
            self.stop()

    def display_data(self, data: dict, addr: tuple):
        """Display parsed FreeD data"""
        # Build output buffer to reduce flicker
        if self.clear_screen:
            output = []
        else:
            output = None

        def add_line(text):
            if output is not None:
                output.append(text)
            else:
                print(text)

        if not self.debug:
            add_line(f"\n{'='*80}")

        # Header with checksum warning if needed (hide if ignoring checksums)
        if self.ignore_checksum:
            checksum_status = ""
            add_line(f"Camera ID: {data['camera_id']} | From: {addr[0]}:{addr[1]}")
        else:
            checksum_status = "✓" if data['checksum_valid'] else "✗ CHECKSUM WARNING"
            add_line(f"Camera ID: {data['camera_id']} | From: {addr[0]}:{addr[1]} | {checksum_status}")
        add_line(f"{'-'*80}")

        # Rotation
        add_line(f"Rotation:")
        if self.convert_units:
            pan_deg = data['pan'] * self.rotation_scale
            tilt_deg = data['tilt'] * self.rotation_scale
            roll_deg = data['roll'] * self.rotation_scale
            add_line(f"  Pan:   {pan_deg:10.2f}°  (raw: {data['pan']:10d})")
            add_line(f"  Tilt:  {tilt_deg:10.2f}°  (raw: {data['tilt']:10d})")
            add_line(f"  Roll:  {roll_deg:10.2f}°  (raw: {data['roll']:10d})")
        else:
            add_line(f"  Pan:   {data['pan']:10d}  (0x{data['pan'] & 0xFFFFFF:06X})")
            add_line(f"  Tilt:  {data['tilt']:10d}  (0x{data['tilt'] & 0xFFFFFF:06X})")
            add_line(f"  Roll:  {data['roll']:10d}  (0x{data['roll'] & 0xFFFFFF:06X})")

        # Position
        add_line(f"\nPosition:")
        if self.convert_units:
            x_mm = data['position']['x'] * self.position_scale
            y_mm = data['position']['y'] * self.position_scale
            z_mm = data['position']['z'] * self.position_scale
            x_m = x_mm / 1000.0
            y_m = y_mm / 1000.0
            z_m = z_mm / 1000.0
            add_line(f"  X:     {x_m:10.3f}m  ({x_mm:10.1f}mm, raw: {data['position']['x']:10d})")
            add_line(f"  Y:     {y_m:10.3f}m  ({y_mm:10.1f}mm, raw: {data['position']['y']:10d})")
            add_line(f"  Z:     {z_m:10.3f}m  ({z_mm:10.1f}mm, raw: {data['position']['z']:10d})")
        else:
            add_line(f"  X:     {data['position']['x']:10d}  (0x{data['position']['x'] & 0xFFFFFF:06X})")
            add_line(f"  Y:     {data['position']['y']:10d}  (0x{data['position']['y'] & 0xFFFFFF:06X})")
            add_line(f"  Z:     {data['position']['z']:10d}  (0x{data['position']['z'] & 0xFFFFFF:06X})")

        # Lens
        add_line(f"\nLens Data:")
        if self.convert_units:
            # Zoom: Piecewise linear interpolation using calibration points
            focal_length = self.interpolate_zoom(data['zoom'])

            # Focus: Piecewise linear interpolation using calibration points
            focus_distance = self.interpolate_focus(data['focus'])

            # Convert meters to feet and inches
            total_inches = focus_distance * 39.3701
            feet = int(total_inches // 12)
            inches = total_inches % 12

            add_line(f"  Zoom:  {focal_length:10.1f}mm focal length  (raw: {data['zoom']:10d})")
            add_line(f"  Focus: {focus_distance:10.2f}m ({feet}ft {inches:.1f}in) (raw: {data['focus']:10d})")
        else:
            add_line(f"  Zoom:  {data['zoom']:10d}  (0x{data['zoom'] & 0xFFFFFF:06X})")
            add_line(f"  Focus: {data['focus']:10d}  (0x{data['focus'] & 0xFFFFFF:06X})")

        # Spare bytes / Timecode
        add_line(f"\nSpare/Timecode:")
        add_line(f"  Value: {data['spare']:10d}  (0x{data['spare']:04X})")
        add_line(f"  Bytes: {data['spare_bytes'].hex(' ').upper()}")

        # Analyze spare byte pattern for timecode detection
        if self.last_spare_value is not None:
            diff = data['spare'] - self.last_spare_value
            if diff == 1:
                self.spare_increment_count += 1
                add_line(f"  Change: +1 (incrementing like timecode) [{self.spare_increment_count} consecutive]")
            elif diff == 0:
                self.spare_same_count += 1
                add_line(f"  Change: 0 (no change) [{self.spare_same_count} consecutive]")
            else:
                add_line(f"  Change: {diff:+d}")
                self.spare_increment_count = 0
                self.spare_same_count = 0
        self.last_spare_value = data['spare']

        # Parse and display timecode if FPS is specified
        if self.timecode_fps:
            timecode = self.parse_timecode(data['spare'], self.timecode_fps)
            add_line(f"  Timecode: {timecode} @ {self.timecode_fps} fps")

            # Timecode validation hint
            if self.spare_increment_count > 10:
                add_line(f"  ✓ LIKELY REAL TIMECODE (incrementing consistently)")

        # Packet size verification
        add_line(f"\nPacket Verification:")
        expected_size = 29
        if data['packet_size'] == expected_size:
            add_line(f"  Size: {data['packet_size']} bytes ✓ (matches FreeD D1 standard)")
        else:
            add_line(f"  Size: {data['packet_size']} bytes ⚠️  (expected {expected_size} bytes)")
            add_line(f"  Difference: {data['packet_size'] - expected_size:+d} bytes")

        # Display extra bytes if present
        if data['extra_bytes'] is not None:
            add_line(f"\n⚠️  EXTRA DATA DETECTED:")
            add_line(f"  Extra bytes: {len(data['extra_bytes'])} bytes")
            add_line(f"  Hex: {data['extra_bytes'].hex(' ').upper()}")
            add_line(f"  ASCII: {' '.join(chr(b) if 32 <= b < 127 else '.' for b in data['extra_bytes'])}")
            add_line(f"  Decimal: {' '.join(str(b) for b in data['extra_bytes'])}")

        # Checksum info (only show if not ignoring checksums)
        if not self.ignore_checksum and not data['checksum_valid']:
            add_line(f"\n⚠️  Checksum: Expected 0x{data['checksum_expected']:02X}, Got 0x{data['checksum_actual']:02X}")

        # Statistics
        if self.ignore_checksum:
            add_line(f"\nPackets: {self.parser.packet_count}")
        else:
            add_line(f"\nPackets: {self.parser.packet_count} valid | {self.parser.error_count} checksum errors")
        add_line(f"Time: {data['timestamp']}")
        add_line(f"{'='*80}")

        # If using clear screen mode, print entire buffer at once to reduce flicker
        if output is not None:
            # Move cursor to home and print everything at once
            print('\033[H' + '\n'.join(output), end='', flush=True)

    def stop(self):
        """Stop the receiver"""
        self.running = False
        if self.socket:
            self.socket.close()
        print(f"\nTotal packets received: {self.parser.packet_count}")
        print(f"Total errors: {self.parser.error_count}")


class FreeDReceiverGUI(FreeDReceiver):
    """FreeDReceiver subclass that stores latest data instead of printing it"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.latest_data = None
        self.latest_addr = None
        self._last_packet_time = None
        self.packet_interval_ms = None   # smoothed ms between packets
        self.packet_fps = None           # smoothed fps
        self._interval_history = deque(maxlen=30)  # rolling window
        self._gl_phase_history = deque(maxlen=8)   # phase counter cycling → locked

    def display_data(self, data: dict, addr: tuple):
        now = time.monotonic()
        if self._last_packet_time is not None:
            interval = (now - self._last_packet_time) * 1000.0
            # Gap >2s means we just reconnected — reset history so fps is clean
            if interval > 2000.0:
                self._interval_history.clear()
                self._gl_phase_history.clear()
            else:
                self._interval_history.append(interval)
                avg = sum(self._interval_history) / len(self._interval_history)
                self.packet_interval_ms = avg
                self.packet_fps = 1000.0 / avg if avg > 0 else None
        self._last_packet_time = now
        # Track genlock phase counter (upper nibble of byte 26)
        rb = data.get('raw_bytes')
        if rb and len(rb) > 26:
            self._gl_phase_history.append((rb[26] >> 4) & 0xF)
        self.latest_data = data
        self.latest_addr = addr


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
        self.receiver = None
        self.recv_thread = None
        self._build_ui()
        self._start_receiver()
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
        self.lbl_port.setText('45000')
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

        mono = QFont(_FONT_MONO, 10)
        for i, (color, hex_ph, field) in enumerate(rows):
            qc = QColor(color)
            for col, text in enumerate([hex_ph, field, '---', '---']):
                item = QTableWidgetItem(text)
                item.setForeground(qc)
                item.setFont(mono)
                tbl.setItem(i, col, item)
            tbl.setRowHeight(i, 26)

        layout.addWidget(tbl)
        self.packet_table = tbl
        self._pm_colors = [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Receiver (background thread)
    # ------------------------------------------------------------------

    def _start_receiver(self):
        self.receiver = FreeDReceiverGUI(
            host='0.0.0.0',
            port=45000,
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
            self.receiver.socket.bind(('0.0.0.0', 45000))
            self.receiver.running = True
        except Exception as e:
            self.lbl_status.setText(f'● ERROR: {e}')
            self.lbl_status.setStyleSheet(f'color: {self.RED}; background: transparent;')
            return

        self.recv_thread = threading.Thread(
            target=self.receiver.receive_loop,
            daemon=True,
            name='FreeDReceiveLoop',
        )
        self.recv_thread.start()

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
            try:
                all_ips = sorted({
                    info[4][0]
                    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                    if not info[4][0].startswith('127.')
                })
                ip_str = '  /  '.join(all_ips) if all_ips else '0.0.0.0'
            except Exception:
                ip_str = '0.0.0.0'
            self.lbl_status.setText(f'● LISTENING :45000  [{ip_str}]')
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

        # Timecode
        tc = r.parse_timecode(data['spare'], 24.0)
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
            mono = QFont(_FONT_MONO, 10)
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

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._timer.stop()
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
