#!/usr/bin/env python3
"""
FreeD Protocol Reader
Reads and parses FreeD camera tracking data from UDP socket

Version : v0.9
Author  : Libor Cevelik
Copyright (c) 2026 Libor Cevelik. All rights reserved.
"""

__version__   = 'v0.9'
__author__    = 'Libor Cevelik'
__copyright__ = 'Copyright (c) 2026 Libor Cevelik'

import socket
import struct
import sys
import threading
import time
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.font as tkfont
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

# Configure UTF-8 encoding for Windows console
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, TypeError):
        pass  # No console in windowed mode — stdout is None


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

    def display_data(self, data: dict, addr: tuple):
        now = time.monotonic()
        if self._last_packet_time is not None:
            interval = (now - self._last_packet_time) * 1000.0
            # Gap >2s means we just reconnected — reset history so fps is clean
            if interval > 2000.0:
                self._interval_history.clear()
            else:
                self._interval_history.append(interval)
                avg = sum(self._interval_history) / len(self._interval_history)
                self.packet_interval_ms = avg
                self.packet_fps = 1000.0 / avg if avg > 0 else None
        self._last_packet_time = now
        self.latest_data = data
        self.latest_addr = addr


class FreeDReaderGUI:
    """Dark dashboard GUI for FreeD Protocol Reader"""

    BG       = '#1a1a2e'
    PANEL_BG = '#16213e'
    BORDER   = '#0f3460'
    DIM      = '#6a6a8a'
    FG       = '#e0e0e0'
    GREEN    = '#00ff88'
    CYAN     = '#00d4ff'
    YELLOW   = '#ffd700'
    ORANGE   = '#ff9500'
    RED      = '#ff4444'

    def __init__(self, root: tk.Tk):
        self.root = root
        self.receiver = None
        self.recv_thread = None
        self._resize_job  = None
        self._base_width  = None   # set after first render
        self._in_rescale  = False
        self._init_fonts()
        self._build_window()
        self._build_layout()
        self._start_receiver()
        self._schedule_update()
        self.root.after(200, self._capture_base_width)

    # ------------------------------------------------------------------
    # Window & Layout
    # ------------------------------------------------------------------

    def _build_window(self):
        self.root.title(f'FreeD Dashboard {__version__}')
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        self.root.minsize(640, 500)
        self.root.attributes('-topmost', True)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.bind('<Configure>', self._on_resize)

    def _init_fonts(self):
        """Create tkfont.Font objects — all widgets reference these, so changing
        size here updates every widget that uses the font automatically."""
        specs = {
            'title':  (_FONT_SANS,  12, 'bold'),
            'cam':    (_FONT_SANS,  10, 'bold'),
            'hdr':    (_FONT_SANS,   9, 'bold'),
            'lbl':    (_FONT_SANS,   9, 'normal'),
            'small':  (_FONT_SANS,   8, 'normal'),
            'big':    (_FONT_SANS,  12, 'bold'),
            'val':    (_FONT_MONO,  13, 'bold'),
            'tc':     (_FONT_MONO,  18, 'bold'),
            'med':    (_FONT_MONO,  11, 'bold'),
            'medr':   (_FONT_MONO,  11, 'normal'),
            'freq':   (_FONT_MONO,  14, 'bold'),
            'hex':    (_FONT_MONO,   9, 'normal'),
            'pm':     (_FONT_MONO,  10, 'normal'),
            'pm_hdr': (_FONT_SANS,   9, 'bold'),
        }
        self._fonts = {
            name: tkfont.Font(family=fam, size=sz, weight=w)
            for name, (fam, sz, w) in specs.items()
        }
        self._font_bases = {name: v[1] for name, v in specs.items()}

    def _rescale(self, pct: float):
        """Scale all fonts to pct% of their base sizes (pct is a plain number, e.g. 130)."""
        factor = pct / 100.0
        for name, base in self._font_bases.items():
            self._fonts[name].configure(size=max(7, int(round(base * factor))))

    def _capture_base_width(self):
        """Record the window width after initial render as the 100% reference."""
        self._base_width = self.root.winfo_width()

    def _on_resize(self, event):
        """Debounced handler for window <Configure>; triggers font rescale."""
        if event.widget is not self.root or self._in_rescale:
            return
        if self._base_width is None:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self._apply_resize, event.width)

    def _apply_resize(self, width):
        """Compute scale % from current vs. base width and rescale all fonts."""
        self._resize_job = None
        self._in_rescale = True
        pct = max(60, min(250, int(width / self._base_width * 100)))
        self._rescale(pct)
        self._in_rescale = False

    def _lf(self, parent, text):
        """Create a styled LabelFrame section"""
        f = tk.LabelFrame(
            parent, text=text,
            bg=self.PANEL_BG, fg=self.DIM,
            font=self._fonts['hdr'],
            bd=1, relief='solid',
            highlightbackground=self.BORDER,
            padx=8, pady=5
        )
        return f

    def _row(self, parent, label_text, color, row):
        """Create a label+value row, return the value label"""
        tk.Label(parent, text=label_text, bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(
            row=row, column=0, sticky='e', pady=2)
        val = tk.Label(parent, text='---', bg=self.PANEL_BG, fg=color,
                       font=self._fonts['val'], width=26, anchor='w')
        val.grid(row=row, column=1, sticky='w', padx=(6, 0), pady=2)
        return val

    def _build_layout(self):
        # ── Header (always visible, above tabs) ──────────────────────
        hdr = tk.Frame(self.root, bg=self.BORDER, pady=4)
        hdr.pack(fill='x')

        tk.Label(hdr, text='FreeD DASHBOARD', bg=self.BORDER, fg=self.FG,
                 font=self._fonts['title']).pack(side='left', padx=10)

        self.lbl_cam = tk.Label(hdr, text='CAM --', bg=self.BORDER, fg=self.YELLOW,
                                font=self._fonts['cam'])
        self.lbl_cam.pack(side='left', padx=8)

        self.lbl_status = tk.Label(hdr, text='● WAITING', bg=self.BORDER, fg=self.DIM,
                                   font=self._fonts['hdr'])
        self.lbl_status.pack(side='right', padx=10)

        tk.Label(hdr, text=f'{__version__}  ·  {__author__}',
                 bg=self.BORDER, fg=self.DIM,
                 font=self._fonts['small']).pack(side='right', padx=12)

        # ── Notebook ─────────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use('default')
        style.configure('D.TNotebook', background=self.BG, borderwidth=0)
        style.configure('D.TNotebook.Tab', background=self.BORDER, foreground=self.DIM,
                        font=self._fonts['hdr'], padding=[12, 4])
        style.map('D.TNotebook.Tab',
                  background=[('selected', self.PANEL_BG)],
                  foreground=[('selected', self.GREEN)])

        nb = ttk.Notebook(self.root, style='D.TNotebook')
        nb.pack(fill='both', expand=True)

        dash = tk.Frame(nb, bg=self.BG)
        nb.add(dash, text='  Dashboard  ')

        pmap = tk.Frame(nb, bg=self.BG)
        nb.add(pmap, text='  Packet Map  ')

        # ── Dashboard: 2-column grid ──────────────────────────────────
        dash.columnconfigure(0, weight=1)
        dash.columnconfigure(1, weight=1)
        gpad = dict(padx=8, pady=5, sticky='nsew')

        # Row 0: ROTATION | POSITION
        rot = self._lf(dash, 'ROTATION')
        rot.grid(row=0, column=0, **gpad)
        self.lbl_pan  = self._row(rot, 'Pan',  self.GREEN, 0)
        self.lbl_tilt = self._row(rot, 'Tilt', self.GREEN, 1)
        self.lbl_roll = self._row(rot, 'Roll', self.GREEN, 2)

        pos = self._lf(dash, 'POSITION')
        pos.grid(row=0, column=1, **gpad)
        self.lbl_x = self._row(pos, 'X', self.CYAN, 0)
        self.lbl_y = self._row(pos, 'Y', self.CYAN, 1)
        self.lbl_z = self._row(pos, 'Z', self.CYAN, 2)

        # Row 1: LENS | GENLOCK
        lens = self._lf(dash, 'LENS')
        lens.grid(row=1, column=0, **gpad)
        self.lbl_zoom  = self._row(lens, 'Zoom',  self.YELLOW, 0)
        self.lbl_focus = self._row(lens, 'Focus', self.YELLOW, 1)

        gl = self._lf(dash, 'GENLOCK')
        gl.grid(row=1, column=1, **gpad)

        self.lbl_gl_status = tk.Label(gl, text='● WAITING', bg=self.PANEL_BG,
                                      fg=self.DIM, font=self._fonts['big'])
        self.lbl_gl_status.grid(row=0, column=0, columnspan=2, pady=(2, 4))

        tk.Label(gl, text='Phase', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=1, column=0, sticky='e')
        self.lbl_gl_phase = tk.Label(gl, text='---', bg=self.PANEL_BG, fg=self.CYAN,
                                     font=self._fonts['med'], anchor='w')
        self.lbl_gl_phase.grid(row=1, column=1, sticky='w', padx=(6, 0))

        tk.Label(gl, text='Ref', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=2, column=0, sticky='e')
        self.lbl_gl_ref = tk.Label(gl, text='---', bg=self.PANEL_BG, fg=self.FG,
                                   font=self._fonts['medr'], anchor='w')
        self.lbl_gl_ref.grid(row=2, column=1, sticky='w', padx=(6, 0))

        tk.Label(gl, text='Freq', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=3, column=0, sticky='e', pady=(4, 0))
        self.lbl_gl_freq = tk.Label(gl, text='--- Hz', bg=self.PANEL_BG, fg=self.ORANGE,
                                    font=self._fonts['freq'], anchor='w')
        self.lbl_gl_freq.grid(row=3, column=1, sticky='w', padx=(6, 0), pady=(4, 0))

        # Row 2: STATUS | RAW PACKET
        tc = self._lf(dash, 'STATUS')
        tc.grid(row=2, column=0, **gpad)

        self.lbl_tc = tk.Label(tc, text='--:--:--:--', bg=self.PANEL_BG,
                               fg=self.ORANGE, font=self._fonts['tc'])
        self.lbl_tc.grid(row=0, column=0, columnspan=2, pady=(2, 4))

        tk.Label(tc, text='Packets', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=1, column=0, sticky='e')
        self.lbl_packets = tk.Label(tc, text='0', bg=self.PANEL_BG, fg=self.FG,
                                    font=self._fonts['medr'], anchor='w')
        self.lbl_packets.grid(row=1, column=1, sticky='w', padx=(6, 0))

        tk.Label(tc, text='Source', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=2, column=0, sticky='e')
        self.lbl_source = tk.Label(tc, text='---', bg=self.PANEL_BG, fg=self.DIM,
                                   font=self._fonts['small'], anchor='w')
        self.lbl_source.grid(row=2, column=1, sticky='w', padx=(6, 0))

        tk.Label(tc, text='Port', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=3, column=0, sticky='e')
        self.lbl_port = tk.Label(tc, text='45000', bg=self.PANEL_BG, fg=self.DIM,
                                 font=self._fonts['small'], anchor='w')
        self.lbl_port.grid(row=3, column=1, sticky='w', padx=(6, 0))

        tk.Label(tc, text='Interval', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=4, column=0, sticky='e', pady=(4, 0))
        self.lbl_interval = tk.Label(tc, text='---', bg=self.PANEL_BG, fg=self.CYAN,
                                     font=self._fonts['med'], anchor='w')
        self.lbl_interval.grid(row=4, column=1, sticky='w', padx=(6, 0), pady=(4, 0))

        raw = self._lf(dash, 'RAW PACKET')
        raw.grid(row=2, column=1, **gpad)

        tk.Label(raw, text='Proto', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=0, column=0, sticky='e', pady=2)
        self.lbl_proto = tk.Label(raw, text='---', bg=self.PANEL_BG, fg=self.CYAN,
                                  font=self._fonts['med'], anchor='w')
        self.lbl_proto.grid(row=0, column=1, sticky='w', padx=(6, 0), pady=2)

        tk.Label(raw, text='Size', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='e').grid(row=1, column=0, sticky='e', pady=2)
        self.lbl_rawsize = tk.Label(raw, text='---', bg=self.PANEL_BG, fg=self.FG,
                                    font=self._fonts['medr'], anchor='w')
        self.lbl_rawsize.grid(row=1, column=1, sticky='w', padx=(6, 0), pady=2)

        tk.Label(raw, text='Hex', bg=self.PANEL_BG, fg=self.DIM,
                 font=self._fonts['lbl'], width=7, anchor='ne').grid(row=2, column=0, sticky='ne', pady=2)
        self.lbl_hex1 = tk.Label(raw, text='', bg=self.PANEL_BG, fg='#aaaaaa',
                                  font=self._fonts['hex'], anchor='w', justify='left')
        self.lbl_hex1.grid(row=2, column=1, sticky='w', padx=(6, 0), pady=(2, 0))
        self.lbl_hex2 = tk.Label(raw, text='', bg=self.PANEL_BG, fg='#aaaaaa',
                                  font=self._fonts['hex'], anchor='w', justify='left')
        self.lbl_hex2.grid(row=3, column=1, sticky='w', padx=(6, 0), pady=(0, 2))

        tk.Frame(dash, bg=self.BG, height=4).grid(row=3, column=0, columnspan=2)

        # ── Packet Map tab ───────────────────────────────────────────
        self._build_packet_map(pmap)

    def _build_packet_map(self, parent):
        """Byte-by-byte packet breakdown table"""
        style = ttk.Style()
        style.configure('PM.Treeview',
                        background=self.PANEL_BG, foreground=self.FG,
                        fieldbackground=self.PANEL_BG, rowheight=22,
                        font=self._fonts['pm'])
        style.configure('PM.Treeview.Heading',
                        background=self.BORDER, foreground=self.FG,
                        font=self._fonts['pm_hdr'], relief='flat')
        style.map('PM.Treeview', background=[('selected', self.BORDER)])

        cols = ('bytes', 'field', 'raw', 'decoded')
        tree = ttk.Treeview(parent, columns=cols, show='headings',
                            style='PM.Treeview', selectmode='none')

        tree.heading('bytes',   text='Hex Bytes')
        tree.heading('field',   text='Field')
        tree.heading('raw',     text='Raw Value')
        tree.heading('decoded', text='Decoded')

        tree.column('bytes',   width=100, anchor='center', stretch=False)
        tree.column('field',   width=100, anchor='w',      stretch=False)
        tree.column('raw',     width=100, anchor='e',      stretch=False)
        tree.column('decoded', width=260, anchor='w',      stretch=True)

        # Row colour tags
        tree.tag_configure('meta',  foreground=self.DIM,    background=self.PANEL_BG)
        tree.tag_configure('rot',   foreground=self.GREEN,  background=self.PANEL_BG)
        tree.tag_configure('pos',   foreground=self.CYAN,   background=self.PANEL_BG)
        tree.tag_configure('lens',  foreground=self.YELLOW, background=self.PANEL_BG)
        tree.tag_configure('spare', foreground=self.ORANGE, background=self.PANEL_BG)
        tree.tag_configure('chk',   foreground=self.DIM,    background=self.PANEL_BG)

        # Static row definitions: (tag, initial hex placeholder, field label)
        rows = [
            ('meta',  '--',         'Msg Type'),
            ('meta',  '--',         'Cam ID'),
            ('rot',   '-- -- --',   'Pan'),
            ('rot',   '-- -- --',   'Tilt'),
            ('rot',   '-- -- --',   'Roll'),
            ('pos',   '-- -- --',   'X'),
            ('pos',   '-- -- --',   'Y'),
            ('pos',   '-- -- --',   'Z'),
            ('lens',  '-- -- --',   'Zoom'),
            ('lens',  '-- -- --',   'Focus'),
            ('spare', '-- --',      'Spare'),
            ('chk',   '--',         'Checksum'),
        ]

        self._map_iids = []
        for tag, hex_ph, field in rows:
            iid = tree.insert('', 'end',
                              values=(hex_ph, field, '---', '---'),
                              tags=(tag,))
            self._map_iids.append(iid)

        tree.pack(fill='both', expand=True, padx=10, pady=10)
        self.packet_tree = tree

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
            self.lbl_status.config(text=f'● ERROR: {e}', fg=self.RED)
            return

        self.recv_thread = threading.Thread(
            target=self.receiver.receive_loop,
            daemon=True,
            name='FreeDReceiveLoop'
        )
        self.recv_thread.start()

    # ------------------------------------------------------------------
    # GUI update loop (10 Hz)
    # ------------------------------------------------------------------

    def _schedule_update(self):
        self._update()
        self.root.after(100, self._schedule_update)

    def _update(self):
        if self.receiver is None:
            return
        data = self.receiver.latest_data
        addr = self.receiver.latest_addr

        if data is None:
            return

        r = self.receiver

        # Stale detection — if no new packet for >2 s, signal loss
        now = time.monotonic()
        is_stale = (r._last_packet_time is not None) and ((now - r._last_packet_time) > 2.0)

        # Rotation
        pan_deg  = data['pan']  * r.rotation_scale
        tilt_deg = data['tilt'] * r.rotation_scale
        roll_deg = data['roll'] * r.rotation_scale
        self.lbl_pan.config( text=f'{pan_deg:+8.2f}°  [{data["pan"]}]')
        self.lbl_tilt.config(text=f'{tilt_deg:+8.2f}°  [{data["tilt"]}]')
        self.lbl_roll.config(text=f'{roll_deg:+8.2f}°  [{data["roll"]}]')

        # Position
        x_m = data['position']['x'] * r.position_scale / 1000.0
        y_m = data['position']['y'] * r.position_scale / 1000.0
        z_m = data['position']['z'] * r.position_scale / 1000.0
        self.lbl_x.config(text=f'{x_m:+7.3f} m  [{data["position"]["x"]}]')
        self.lbl_y.config(text=f'{y_m:+7.3f} m  [{data["position"]["y"]}]')
        self.lbl_z.config(text=f'{z_m:+7.3f} m  [{data["position"]["z"]}]')

        # Lens — FreeD standard: raw / 1000 = mm (zoom) and m (focus)
        focal_length   = data['zoom']  / 1000.0
        focus_distance = data['focus'] / 1000.0
        total_inches   = focus_distance * 39.3701
        feet           = int(total_inches // 12)
        frac_in        = total_inches % 12
        lens_color = self.DIM if is_stale else self.YELLOW
        self.lbl_zoom.config( text=f'{focal_length:.1f} mm  [{data["zoom"]}]',  fg=lens_color)
        self.lbl_focus.config(text=f'{focus_distance:.2f}m  {feet}ft {frac_in:.1f}in  [{data["focus"]}]', fg=lens_color)

        # Timecode
        tc = r.parse_timecode(data['spare'], 24.0)
        self.lbl_tc.config(text=tc or '--:--:--:--')

        # Stats
        self.lbl_packets.config(text=f"{r.parser.packet_count:,}")
        self.lbl_cam.config(text=f"CAM {data['camera_id']}")
        if addr:
            self.lbl_source.config(text=f'{addr[0]}:{addr[1]}')
        if is_stale:
            self.lbl_status.config(text='● TIMEOUT', fg=self.RED)
        else:
            self.lbl_status.config(text='● LIVE', fg=self.GREEN)

        # Packet interval
        if r.packet_interval_ms is not None:
            fps = r.packet_fps
            self.lbl_interval.config(text=f'{r.packet_interval_ms:.1f} ms  ({fps:.1f} fps)')

        # Raw packet
        msg_type = data['message_type']
        proto_name = f'D{msg_type & 0x0F}  (0x{msg_type:02X})'
        self.lbl_proto.config(text=proto_name)
        self.lbl_rawsize.config(text=f"{data['packet_size']} bytes")
        raw = data['raw_bytes']
        mid = len(raw) // 2
        line1 = ' '.join(f'{b:02X}' for b in raw[:mid])
        line2 = ' '.join(f'{b:02X}' for b in raw[mid:])
        self.lbl_hex1.config(text=line1)
        self.lbl_hex2.config(text=line2)

        # Genlock status (spare bytes)
        rb = data['raw_bytes']
        gl_lower = rb[26] & 0x0F        # lower nibble — lock flags
        gl_phase = (rb[26] >> 4) & 0xF  # upper nibble — frame phase counter
        gl_ref   = rb[27]               # reference format code
        if is_stale:
            self.lbl_gl_status.config(text='● NO SIGNAL', fg=self.RED)
            self.lbl_gl_phase.config(text='---')
            self.lbl_gl_freq.config(text='--- Hz')
        else:
            is_locked = bool(gl_lower & 0x01)
            self.lbl_gl_status.config(
                text='● LOCKED' if is_locked else '● UNLOCKED',
                fg=self.GREEN if is_locked else self.RED)
            self.lbl_gl_phase.config(text=f'{gl_phase:X}h  ({gl_phase}/16)')
            if r.packet_fps is not None:
                self.lbl_gl_freq.config(text=f'{r.packet_fps:.2f} Hz')
        self.lbl_gl_ref.config(text=f'0x{gl_ref:02X} (vendor-defined)')

        # Packet Map tab — update every row with live data
        if hasattr(self, 'packet_tree'):
            rb = data['raw_bytes']
            gl_lower_pm = rb[26] & 0x0F
            gl_phase_pm = (rb[26] >> 4) & 0xF
            lock_str    = 'LOCKED' if (gl_lower_pm & 0x01) else 'UNLOCKED'
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
                 f'{focal_length:.1f} mm'),
                (' '.join(f'{b:02X}' for b in rb[23:26]),
                 'Focus', str(data['focus']),
                 f'{focus_distance:.2f}m  {feet}ft {frac_in:.1f}in'),
                (f'{rb[26]:02X} {rb[27]:02X}',
                 'Spare/GL', f'0x{data["spare"]:04X}',
                 f'{lock_str}  ph={gl_phase_pm:X}h  ref=0x{rb[27]:02X}'),
                (f'{rb[28]:02X}',
                 'Checksum', f'0x{rb[28]:02X}',
                 'OK' if data['checksum_valid'] else 'MISMATCH'),
            ]
            for i, (hx, field, raw_val, decoded) in enumerate(map_rows):
                self.packet_tree.item(self._map_iids[i],
                                      values=(hx, field, raw_val, decoded))

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close(self):
        if self.receiver:
            self.receiver.running = False
            if self.receiver.socket:
                try:
                    self.receiver.socket.close()
                except Exception:
                    pass
        self.root.destroy()


def main_gui():
    """GUI entry point — launches dark dashboard with baked-in defaults"""
    root = tk.Tk()
    FreeDReaderGUI(root)
    root.mainloop()


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
