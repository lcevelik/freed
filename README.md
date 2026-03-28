# FreeD Dashboard

A PyQt6 dark-theme GUI application for receiving, parsing, and analysing camera tracking data from the **FreeD (D1) protocol** over UDP.

![Version](https://img.shields.io/badge/version-v1.9-orange) ![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![PyQt6](https://img.shields.io/badge/PyQt6-6.x-green) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Features

- Real-time FreeD D1 packet reception over UDP
- Apple-dark PyQt6 GUI with four tabs:
  - **Dashboard** — live rotation, position, lens, genlock, timecode, and status
  - **Packet Map** — byte-by-byte protocol breakdown with decoded values
  - **Jitter** — timing health banner, numeric noise monitoring, and full reference guide
  - **Settings** — configure UDP port, destinations, frame rate, and OpenTrackIO output
- Correct checksum validation — device-verified formula `(byte26 + byte27 + byte28) & 0xFF == 0xF6`
- Parses 29-byte FreeD D1 packets with unit conversion (degrees, meters, mm)
- Timecode decoding from spare bytes (24 fps default)
- Genlock phase detection and lock status
- **OpenTrackIO v1.0.1** output — forwards tracking data as JSON over UDP
- Settings persist across restarts via `%APPDATA%\FreeDReader\`
- Standalone `.exe` build via PyInstaller (no Python required on target)
- Included **FreeD Simulator** for development and testing without real hardware

---

## Requirements

### Running from source

| Package | Version |
|---------|---------|
| Python  | 3.8+    |
| PyQt6   | 6.x     |
| numpy   | 1.x / 2.x |

```bash
pip install PyQt6 numpy
```

### Running the portable executable

No dependencies — copy `dist\FreeDReader_v1.9.exe` to any Windows machine and run it.

---

## Usage

### GUI (default)

```bash
python freed_reader.py
```

### CLI / headless mode

```bash
python freed_reader.py --cli [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `0.0.0.0` | IP address to listen on |
| `--port` | `45000` | UDP port |
| `--debug` / `-d` | off | Show raw packet bytes |
| `--timecode` / `-t` | `24.0` | Timecode FPS for spare-byte decoding |
| `--convert` / `-c` | on | Convert to real-world units |
| `--ignore-checksum` / `-i` | on | Parse packets even on checksum mismatch |

### Simulator

Sends synthetic FreeD packets to localhost for testing:

```bash
python freed_simulator.py
```

---

## Tabs

### Dashboard

Live camera data in card layout:

| Card | Fields |
|------|--------|
| ROTATION | Pan, Tilt, Roll (degrees) |
| POSITION | X, Y, Z (meters + raw) |
| LENS | Zoom (mm), Focus (m + ft/in) |
| GENLOCK | Lock status, phase counter, frequency |
| STATUS | Timecode, packet count, source IP, interval |
| RAW PACKET | Protocol type, size, hex dump |

### Packet Map

Table showing every byte of the latest packet — hex, field name, raw value, and decoded value — colour-coded by field type.

### Jitter

Inter-packet timing analysis updated at 10 Hz, split into two sub-tabs:

**Monitor**

| Section | Stats |
|---------|-------|
| Timing | Mean interval, Std Dev, Min/Max, Peak ±, RFC 3550 jitter — all with colour-coded health LED |
| Position Noise | X, Y, Z standard deviation in mm (rolling 500-packet window) |
| Rotation Noise | Pan, Tilt, Roll standard deviation in degrees (rolling 500-packet window) |

Health LED thresholds:

| Colour | Timing | Position | Rotation |
|--------|--------|----------|----------|
| Green (IDEAL) | < 1 ms | < 0.1 mm | < 0.01° |
| Yellow (ACCEPTABLE) | < 3 ms | < 0.5 mm | < 0.05° |
| Orange (MARGINAL) | < 5 ms | < 1.0 mm | < 0.10° |
| Red (PROBLEMATIC) | ≥ 5 ms | ≥ 1.0 mm | ≥ 0.10° |

**Reference**

Scrollable in-app documentation covering what each metric means, common causes of poor jitter, and how to fix them.

### Settings

- Change the UDP listen port at runtime — hit **Apply** to rebind without restarting
- Add / remove forwarding destinations (IP + port)
- Configure timecode frame rate
- Enable **OpenTrackIO** output with custom IP, port, and subject name

All settings are saved automatically to `%APPDATA%\FreeDReader\freed_forwarder_config.json` and restored on next launch.

---

## FreeD D1 Packet Structure

29-byte UDP packet:

| Bytes | Field | Scale | Unit |
|-------|-------|-------|------|
| 0 | Message type | — | 0xD1 |
| 1 | Camera ID | — | integer |
| 2–4 | Pan | ÷ 32768 | degrees |
| 5–7 | Tilt | ÷ 32768 | degrees |
| 8–10 | Roll | ÷ 32768 | degrees |
| 11–13 | X position | ÷ 64 ÷ 1000 | meters |
| 14–16 | Y position | ÷ 64 ÷ 1000 | meters |
| 17–19 | Z position | ÷ 64 ÷ 1000 | meters |
| 20–22 | Zoom | ÷ 1000 | mm focal length |
| 23–25 | Focus | ÷ 1000 | meters |
| 26–27 | Spare / Genlock | upper nibble = phase | timecode / genlock |
| 28 | Checksum | `(byte26 + byte27 + byte28) & 0xFF == 0xF6` | — |

> **Checksum note:** The device uses a spare-byte complement scheme, not a standard XOR. The formula was determined by live packet capture and verified across 200+ packets.

Optional 4-byte extension (bytes 29–32): full H:M:S:F timecode block, injected by the forwarder when timecode injection is enabled.

---

## Project Structure

```
freed/
├── freed_reader.py        # Main GUI application + forwarder
├── protocol.py            # FreeDParser, FreeDReceiver, FreeDReceiverGUI
├── opentrackio.py         # OpenTrackIOSender (JSON over UDP, v1.0.1)
├── freed_simulator.py     # Test packet generator
├── tests/
│   └── test_freed.py      # 48 unit tests
├── FreeDReader_v1.9.spec  # PyInstaller build spec
└── dist/
    └── FreeDReader_v1.9.exe  # Standalone executable
```

---

## Building the Executable

```bash
pip install pyinstaller
pyinstaller FreeDReader_v1.9.spec
```

Output: `dist\FreeDReader_v1.9.exe` (~50 MB, fully self-contained, no Python required)

---

## Troubleshooting

**No packets received**
- Verify the FreeD source is targeting the correct IP and port (default 45000)
- Check Windows Firewall allows inbound UDP on the configured port
- Ensure no other app is bound to the same port (close FreeDReader before running diagnostic scripts)

**Wrong port**
- Open the **Settings** tab, enter the correct port, and click **Apply**

**Checksum shows MISMATCH**
- Upgrade to v1.9 — earlier versions used an incorrect XOR algorithm. v1.9 uses the correct device-verified formula.

**High jitter (10 ms+)**
- Windows timer resolution: v1.9 sets 1 ms resolution at startup automatically
- If jitter persists, it is likely genuine source or network jitter — check the Jitter → Reference tab for diagnosis guidance

**Settings not saving**
- Ensure the app has write access to `%APPDATA%\FreeDReader\`
- Upgrade to v1.6+ — earlier versions stored config next to the EXE which could fail on restricted paths

---

## Changelog

### v1.9 — 2026-03-28

**Bug fixes**

- **Fixed checksum algorithm** — live packet capture revealed the device uses `(byte26 + byte27 + byte28) & 0xFF == 0xF6`, not XOR of bytes 0–27. Checksum now shows OK on every valid packet.
- **Windows timer resolution** — `timeBeginPeriod(1)` called at startup sets 1 ms OS scheduler tick, eliminating the 15.6 ms Windows default timer noise from jitter measurements.

**New features**

- **Position noise monitoring** — rolling 500-packet standard deviation for X, Y, Z displayed in mm with colour-coded health LED
- **Rotation noise monitoring** — rolling 500-packet standard deviation for Pan, Tilt, Roll displayed in degrees with colour-coded health LED
- Jitter graphs removed in favour of pure numeric display — cleaner and more precise

---

### v1.8 — 2026-03-27

- Added **Jitter Reference** sub-tab — scrollable in-app documentation covering all metrics, causes of poor signal quality, and remediation steps

---

### v1.7 — 2026-03-27

- Added **jitter health banner** with colour-coded LED indicators (IDEAL / ACCEPTABLE / MARGINAL / PROBLEMATIC) for all timing, position, and rotation metrics

---

### v1.6 — 2026-03-27

- Settings now stored in `%APPDATA%\FreeDReader\` — survive EXE moves, reinstalls, and restricted install paths
- All settings (listen port, destinations, frame rate, OpenTrackIO config) persist correctly across app restarts

---

### v1.4–v1.5 — 2026-03-27

- Codebase split into `protocol.py` (parser + receiver) and `opentrackio.py` (OpenTrackIO sender) for maintainability
- OpenTrackIO sequence number fix — was double-incrementing per send; now increments once
- 48 unit tests covering parser, packet builder, checksum, interpolation, forwarder config, TC injection, and OpenTrackIO output

---

### v1.1–v1.3 — 2026-03-27

- **Jitter tab** — inter-packet timing analysis with RFC 3550 jitter, std dev, min/max, peak
- **Settings tab** — runtime UDP port change without app restart
- Listen port persists across restarts

---

### v1.0 — initial release

- PyQt6 dark-theme GUI dashboard
- Dashboard tab: rotation, position, lens, genlock, timecode, status, raw packet
- Packet Map tab: byte-by-byte protocol breakdown
- FreeD D1 packet parser with checksum validation
- UDP receiver with 10 Hz UI update loop
- FreeD Simulator for testing without real hardware
- Standalone `.exe` via PyInstaller

---

## Author

**Libor Cevelik** — Copyright © 2026
