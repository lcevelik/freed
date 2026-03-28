"""
FreeD Protocol — parser, UDP receiver, GUI-aware receiver subclass.
"""

import socket
import sys
import struct
import time
from collections import deque
from datetime import datetime


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
        return int.from_bytes(data[:3], byteorder='big', signed=True)

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
        calculated_checksum = self.calculate_checksum(data[:28])
        packet_checksum = data[28]

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

        # Extended TC block: bytes 29–32 (H, M, S, F — one byte each)
        ext_tc = None
        if len(data) >= 33:
            ext_tc = (data[29], data[30], data[31], data[32])

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
            'ext_tc': ext_tc,
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
        Parse timecode from spare bytes value using H:M:S bit-pack encoding:
          bits [15:11] = hours   (5 bits)
          bits [10:5]  = minutes (6 bits)
          bits [4:0]   = seconds // 2 (5 bits, multiply by 2 to recover)
        Returns timecode in HH:MM:SS:FF format (frames always 00).
        """
        if fps is None or fps <= 0:
            return None
        hours   = (spare_value >> 11) & 0x1F
        minutes = (spare_value >>  5) & 0x3F
        seconds = (spare_value &  0x1F) * 2
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:00"

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
        self._interval_history = deque(maxlen=30)   # rolling window (avg/fps)
        self._gl_phase_history = deque(maxlen=8)    # phase counter cycling → locked
        self._jitter_history   = deque(maxlen=500)  # long history for jitter tab
        self._rfc_jitter       = 0.0                # RFC 3550-style jitter accumulator
        self._prev_interval    = None               # previous interval for RFC diff
        # Position noise history (raw values — convert with position_scale / 1000 for metres)
        self._x_history        = deque(maxlen=500)
        self._y_history        = deque(maxlen=500)
        self._z_history        = deque(maxlen=500)
        # Rotation noise history (raw values — convert with rotation_scale for degrees)
        self._pan_history      = deque(maxlen=500)
        self._tilt_history     = deque(maxlen=500)
        self._roll_history     = deque(maxlen=500)
        self.on_packet         = None               # optional callback(raw_bytes)
        self.on_packet_parsed  = None               # optional callback(data_dict)

    def display_data(self, data: dict, addr: tuple):
        now = time.monotonic()
        if self._last_packet_time is not None:
            interval = (now - self._last_packet_time) * 1000.0
            # Gap >2s means we just reconnected — reset history so fps is clean
            if interval > 2000.0:
                self._interval_history.clear()
                self._gl_phase_history.clear()
                self._jitter_history.clear()
                self._x_history.clear(); self._y_history.clear(); self._z_history.clear()
                self._pan_history.clear(); self._tilt_history.clear(); self._roll_history.clear()
                self._rfc_jitter    = 0.0
                self._prev_interval = None
            else:
                self._interval_history.append(interval)
                avg = sum(self._interval_history) / len(self._interval_history)
                self.packet_interval_ms = avg
                self.packet_fps = 1000.0 / avg if avg > 0 else None
                self._jitter_history.append(interval)
                if self._prev_interval is not None:
                    d = abs(interval - self._prev_interval)
                    self._rfc_jitter += (d - self._rfc_jitter) / 16.0
                self._prev_interval = interval
        self._last_packet_time = now
        # Track position and rotation for noise/jitter analysis
        pos = data.get('position', {})
        self._x_history.append(pos.get('x', 0))
        self._y_history.append(pos.get('y', 0))
        self._z_history.append(pos.get('z', 0))
        self._pan_history.append(data.get('pan', 0))
        self._tilt_history.append(data.get('tilt', 0))
        self._roll_history.append(data.get('roll', 0))
        # Track genlock phase counter (upper nibble of byte 26)
        rb = data.get('raw_bytes')
        if rb and len(rb) > 26:
            self._gl_phase_history.append((rb[26] >> 4) & 0xF)
        self.latest_data = data
        self.latest_addr = addr
        if self.on_packet is not None:
            try:
                self.on_packet(data.get('raw_bytes', b''))
            except Exception:
                pass
        if self.on_packet_parsed is not None:
            try:
                self.on_packet_parsed(data)
            except Exception:
                pass
