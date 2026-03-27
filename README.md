# FreeD Dashboard

A PyQt6 dark-theme GUI application for receiving, parsing, and analysing camera tracking data from the **FreeD (D1) protocol** over UDP.

![Version](https://img.shields.io/badge/version-v1.1-orange) ![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![PyQt6](https://img.shields.io/badge/PyQt6-6.x-green) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Features

- Real-time FreeD D1 packet reception over UDP
- Apple-dark PyQt6 GUI with four tabs:
  - **Dashboard** — live rotation, position, lens, genlock, timecode, and status
  - **Packet Map** — byte-by-byte protocol breakdown with decoded values
  - **Jitter** — inter-packet timing analysis with live graphs and stats
  - **Settings** — configure UDP port without restarting the app
- Validates XOR checksums (configurable ignore mode)
- Parses 29-byte FreeD D1 packets with unit conversion (degrees, meters, mm)
- Timecode decoding from spare bytes (24 fps default)
- Genlock phase detection and lock status
- Standalone `.exe` build via PyInstaller (no Python required on target)
- Included **FreeD Simulator** for development and testing without real hardware

---

## Requirements

### Running from source

| Package | Version |
|---------|---------|
| Python  | 3.8+    |
| PyQt6   | 6.x     |
| pyqtgraph | 0.14+ |
| numpy   | 1.x / 2.x |

```bash
pip install PyQt6 pyqtgraph numpy
```

### Running the portable executable

No dependencies — copy `dist\FreeDReader.exe` to any Windows machine and run it.

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

Inter-packet timing analysis updated at 10 Hz:

| Stat | Description |
|------|-------------|
| Mean | Average interval (ms) |
| Std Dev | Standard deviation — classic jitter measure |
| Min / Max | Fastest and slowest intervals seen |
| Peak ± | Maximum deviation from mean |
| RFC Jitter | RFC 3550-style running jitter accumulator |

- **Line graph** — rolling last 200 intervals with mean reference line
- **Histogram** — distribution of last 500 intervals in 30 bins

### Settings

Change the UDP listen port at runtime without restarting. Hit **Apply** to rebind the receiver to the new port. The Dashboard status bar updates automatically.

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
| 28 | Checksum | XOR bytes 0–27 | — |

---

## Building the Executable

Requires PyInstaller:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --name FreeDReader --noconsole freed_reader.py
```

Output: `dist\FreeDReader.exe` (~50 MB, fully self-contained)

Or use the included batch file:

```
BUILD_EXECUTABLE.bat
```

---

## Troubleshooting

**No packets received**
- Verify the FreeD source is targeting the correct IP and port (default 45000)
- Check Windows Firewall allows inbound UDP on the configured port
- Ensure no other app is bound to the same port

**Wrong port**
- Open the **Settings** tab, enter the correct port, and click **Apply**

**Checksum errors**
- Checksum validation is ignored by default — data is always displayed
- Verify the source uses FreeD D1 format (0xD1 message type, 29-byte packets)

---

## Project Structure

```
freed/
├── freed_reader.py        # Main GUI application
├── freed_simulator.py     # Test packet generator
├── run_gui.bat            # Quick launcher (double-click)
├── BUILD_EXECUTABLE.bat   # PyInstaller build script
└── dist/
    └── FreeDReader.exe    # Standalone executable
```

---

## Changelog

### v1.1 — 2026-03-27

**New features**

- **Jitter tab** — dedicated inter-packet timing analysis panel:
  - Six real-time stats: Mean, Std Dev, Min, Max, Peak ±, and RFC 3550 jitter
  - Rolling line graph of the last 200 packet intervals with a mean reference line
  - Interval distribution histogram over the last 500 packets (30 bins)
  - Powered by pyqtgraph and numpy for smooth, low-overhead rendering
- **Settings tab** — configure the UDP listen port at runtime:
  - Spinbox (range 1024–65535, default 45000) with an Apply button
  - Receiver restarts on the new port without closing the app
  - Dashboard port label updates automatically on Apply
- **run_gui.bat** — double-click launcher for quick startup without a terminal

**Improvements**

- Added `PYQTGRAPH_QT_LIB=PyQt6` environment hint to prevent Qt binding conflicts when both PyQt5 and PyQt6 are installed
- `window.raise_()` and `window.activateWindow()` ensure the window comes to front on launch
- README fully rewritten with tab documentation, packet structure table, jitter metric descriptions, troubleshooting, and build instructions

**Dependencies added**

- `pyqtgraph >= 0.14`
- `numpy >= 1.x`

---

### v1.0 — initial release

- PyQt6 dark-theme GUI dashboard
- Dashboard tab: rotation, position, lens, genlock, timecode, status, raw packet
- Packet Map tab: byte-by-byte protocol breakdown
- FreeD D1 packet parser with XOR checksum validation
- UDP receiver with 10 Hz UI update loop
- FreeD Simulator for testing without real hardware
- Standalone `.exe` via PyInstaller

---

## Author

**Libor Cevelik** — Copyright © 2026
