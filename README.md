# FreeD Dashboard

A PyQt6 dark-theme GUI application for receiving, parsing, and analysing camera tracking data from the **FreeD (D1)** and **OpenTrackIO** protocols over UDP.

![Version](https://img.shields.io/badge/version-v3.0.0-orange) ![Python](https://img.shields.io/badge/Python-3.8%2B-blue) ![PyQt6](https://img.shields.io/badge/PyQt6-6.x-green) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Features

- Real-time FreeD D1 packet reception over UDP
- **OpenTrackIO input** (e.g. Sony Ocellus) — segmented messages reassembled, checksum-verified, lens / distortion / timing / device metadata decoded
- Input mode: **FreeD**, **OpenTrackIO**, **Both** (separate ports) or **Auto-detect** — every datagram is identified from its header
- Detected-senders table with optional lock to one sender IP; wrong-protocol-on-port warning with one-click switch
- **OpenTrackIO JSON and CBOR** input (CBOR via the `cbor2` package, bundled in the EXE)
- **Recorder & analyzer** — record 10–60 s of everything arriving on the listening ports, see which fields each stream actually carries (Live / Fixed / Zero / Not sent), save and reload recordings, export the analysis as CSV
- Apple-dark PyQt6 GUI with five tabs:
  - **Dashboard** — live rotation, position, lens, genlock, timecode, and status (follows the active source)
  - **Jitter** — timing health banner, numeric noise monitoring, genlock health, and full reference guide
  - **Packet Map** — sub-tabs **FreeD** (byte-by-byte breakdown with decoded values) and **OpenTrackIO** (device, transform, lens, distortion, timing, stream statistics)
  - **Recorder** — record, analyse, save / load recordings
  - **Settings** — configure input, destinations, frame rate, timecode source, and OpenTrackIO output
- Checksum validation — standard FreeD `(0x40 - sum of bytes 0–27) & 0xFF` (Sony Ocellus, Unreal) and the legacy `(byte26 + byte27 + byte28) & 0xFF == 0xF6` are both accepted; packets the app writes use the standard checksum
- Parses 29-byte FreeD D1 packets with unit conversion (degrees, meters, mm)
- Timecode from system clock (H:M:S:F) displayed live in the Timing tab and Settings
- Genlock phase detection, lock status, and genlock-aware jitter health assessment
- **OpenTrackIO v1.0.1** output — forwards tracking data as JSON over UDP
- **OpenTrackIO Simulator** — send synthetic OpenTrackIO JSON for testing without real hardware
- Send rate automatically follows configured timecode FPS
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
| cbor2   | 5.x+ (only for CBOR OpenTrackIO input) |

```bash
pip install PyQt6 numpy cbor2
```

### Running the portable executable

No dependencies — copy `dist\FreeD_Reader_V3.0.0.exe` to any Windows machine and run it.

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

### Recorder

- Pick a length (10 / 20 / 30 / 60 s) and press **Record**; **Stop** ends early. Every packet on the listening ports is captured, including senders the live view filters out. The live view keeps running.
- When recording ends, each stream (sender + protocol) is analysed: samples, rate, interval mean ± std, checksum errors, lost / incomplete samples, encoding, and every field marked:

| Status | Meaning |
|--------|---------|
| Live | more than one value seen — the range is shown |
| Fixed | one meaningful value |
| Zero | field present but always 0 / "N/A" / empty |
| Not sent | an OpenTrackIO reference field never present |

- **Gaps only** shows just the Zero and Not sent fields.
- **Save…** writes a `.fdrec` file (default folder `Documents\FreeD Recordings`); **Load…** reopens one for analysis; **Export CSV…** writes the field table of the selected stream.
- `.fdrec` files are JSON Lines holding the raw datagrams with timestamps, so a recording can be re-analysed later with newer versions of the app. A 20 s Ocellus recording is about 1 MB.

### Settings

- **Network** — choose the input mode and ports; hit **Apply ports** to rebind without restarting. The *Detected senders* table lists everything arriving (protocol, device, rate, status); *accept from* locks a protocol to one sender IP
- Add / remove forwarding destinations (IP + port)
- Configure timecode frame rate
- Enable **OpenTrackIO** output with custom IP, port, and subject name

### OpenTrackIO input

| Input mode | Listens on | Active source |
|------------|-----------|---------------|
| FreeD (default) | FreeD port | FreeD |
| OpenTrackIO | OpenTrackIO port | OpenTrackIO |
| Both | each protocol on its own port (one socket if the ports are equal) | picked in the header |
| Auto-detect | FreeD port, either protocol | whichever is arriving |

The active source drives the Dashboard, Packet Map, Jitter tab and all outputs:

| Active source | Forwarding destinations | OpenTrackIO output |
|---------------|------------------------|--------------------|
| FreeD | FreeD packets (+ TC injection if enabled) | generated from FreeD |
| OpenTrackIO | converted to 29-byte FreeD D1, **no timecode** | original packets relayed unchanged |

OTI → FreeD conversion matches the Sony Ocellus's own FreeD output: zoom / focus = raw lens encoder counts (`lens.rawEncoders`, 0–65535), standard FreeD checksum. The Dashboard shows the physical focal length and focus distance from OpenTrackIO.
Genlock lock state is carried in the byte-26 phase counter. The relay pauses automatically if the output would loop back into this app's own OpenTrackIO port.

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
| 28 | Checksum | `(0x40 - sum of bytes 0–27) & 0xFF` | — |

> **Checksum note:** The standard FreeD checksum is verified on every packet from a Sony Ocellus (722/722 in a live capture). An earlier device used a spare-byte scheme, `(byte26 + byte27 + byte28) & 0xFF == 0xF6`; it is still accepted on input. The Packet Map shows which scheme matched.
>
> **Zoom / focus units** are sender-specific. Sony Ocellus sends raw lens encoder counts (0–65535); the `÷ 1000` column reflects the earlier device's mm / m convention.

Optional 4-byte extension (bytes 29–32): full H:M:S:F timecode block, injected by the forwarder when timecode injection is enabled.

---

## Project Structure

```
freed/
├── freed_reader.py              # Main GUI application (FreeDDashboard)
├── src/
│   ├── protocol.py              # FreeDParser, FreeDReceiver(GUI), UdpListener, detect_protocol
│   ├── opentrackio.py           # OpenTrackIOSender + OpenTrackIOParser, OTI → FreeD conversion
│   ├── forwarder.py             # FreeDForwarder (destinations, TC injection, config)
│   ├── recorder.py              # Recorder, Recording (.fdrec), analyze()
│   ├── ltc_reader.py            # BluefishLTCReader
│   └── ui_utils.py              # Fonts, stdout redirect
├── simulators/
│   ├── freed_simulator.py       # FreeD test packet generator
│   └── opentrackio_simulator.py # OpenTrackIO JSON test sender
├── tests/
│   ├── test_freed.py             # FreeD parser / forwarder / OTI output tests
│   ├── test_opentrackio_input.py # OpenTrackIO input, reassembly, conversion, listener tests
│   ├── test_recorder.py          # recorder, .fdrec files, field analysis (JSON + CBOR)
│   └── data/ocellus_sample.json  # captured Ocellus sample (test fixture)
├── scripts/                     # launch / build batch files
├── specs/                       # PyInstaller specs (FreeD_Reader_V3.0.0.spec is current)
└── dist/
    └── FreeD_Reader_V3.0.0.exe  # Standalone executable
```

---

## Building the Executable

```bash
pip install pyinstaller cbor2
pyinstaller specs/FreeD_Reader_V3.0.0.spec
```

Output: `dist\FreeD_Reader_V3.0.0.exe` (~50 MB, fully self-contained, no Python required)

---

## Troubleshooting

**No packets received**
- Verify the FreeD source is targeting the correct IP and port (default 45000)
- Check Windows Firewall allows inbound UDP on the configured port
- Ensure no other app is bound to the same port (close FreeDReader before running diagnostic scripts)

**Wrong port / wrong protocol**
- Open **Settings → Network**, enter the correct port, and click **Apply ports**
- If an orange banner says a protocol is arriving on a port set for the other one, click its button to switch
- The *Detected senders* table shows every sender reaching the listening ports and why it is (or is not) being used

**Checksum shows MISMATCH**
- Both the standard FreeD checksum and the legacy `0xF6` scheme are accepted; a mismatch under both means the packet is corrupt or uses another vendor scheme

**High jitter (10 ms+)**
- Windows timer resolution: v1.9 sets 1 ms resolution at startup automatically
- Readings quantised in ~15.6 ms steps came from `time.monotonic()` (15.6 ms resolution on Windows before Python 3.13); packets are now timestamped with `time.perf_counter()`
- If jitter persists, it is likely genuine source or network jitter — check the Jitter → Reference tab for diagnosis guidance

**CBOR stream shows decode errors**
- Install the CBOR decoder: `pip install cbor2` (the v3.0.0 EXE already includes it)

**Settings not saving**
- Ensure the app has write access to `%APPDATA%\FreeDReader\`
- Upgrade to v1.6+ — earlier versions stored config next to the EXE which could fail on restricted paths

---

## Changelog

### v3.0.0 — 2026-09-29

**New features**

- **Recorder tab** — record 10–60 s of raw input from every listening port, analyse each stream field by field (Live / Fixed / Zero / Not sent), save / load `.fdrec` recordings, export the analysis to CSV
- **CBOR OpenTrackIO** — `cbor2` is now a dependency and bundled in the EXE; a clear error is shown if it is missing
- **OpenTrackIO input** — receive OpenTrackIO v1.0.x (JSON and CBOR) directly from devices such as the Sony Ocellus ASR-CT1. Segmented messages are reassembled, Fletcher-16 verified, and lost / incomplete samples are counted
- **Input mode** — FreeD / OpenTrackIO / Both (separate ports) / Auto-detect, with per-datagram protocol detection
- **Detected senders** table, per-protocol sender lock, and a wrong-protocol warning with one-click switch
- **Packet Map › OpenTrackIO** — device, transform, lens, distortion, timing/genlock, stream statistics
- Tab order is now Dashboard · Jitter · Packet Map · Recorder · Settings; Packet Map has FreeD / OpenTrackIO sub-tabs
- **OpenTrackIO → FreeD** — converted 29-byte D1 packets (no timecode) go to the existing forwarding destinations; incoming OpenTrackIO is relayed unchanged to the OpenTrackIO output
- Header badge shows the active source; source picker in Both mode

**Bug fixes**

- **Jitter measurement resolution** — timestamps used `time.monotonic()`, which only ticks every 15.6 ms on Windows with Python < 3.13, adding up to ~8 ms of false jitter. Now `time.perf_counter()` (sub-microsecond)
- GUI receive buffer raised from 1024 bytes to 64 KB so large datagrams are never truncated
- **FreeD checksum** — the standard FreeD checksum `(0x40 - sum of bytes 0–27)` is now accepted (Sony Ocellus FreeD showed MISMATCH on every packet); the legacy `0xF6` scheme is still accepted. TC injection, OTI → FreeD conversion and the simulator now **write** the standard checksum, so receivers such as Unreal accept them
- **OTI → FreeD lens fields** now carry the raw lens encoder counts, matching the Ocellus's own FreeD output (previously mm × 1000)

### v2.0.0 — 2026-05-12

**Project restructure**

- Source files reorganised into subfolders: `src/` (protocol, forwarder, LTC reader, OpenTrackIO, UI utils), `simulators/`, `scripts/`, `specs/`, `docs/`
- Extracted shared font selection and Windows stdout/stderr redirect into `src/ui_utils.py` — eliminates copy-pasted boilerplate across all GUI files
- `BluefishLTCReader` moved to `src/ltc_reader.py`; `FreeDForwarder` moved to `src/forwarder.py`
- Removed unused `import struct` from forwarder; removed dead `pyqtgraph` import from main module
- Fixed module docstring version (was `v1.0`); updated to reflect dashboard purpose
- `scripts/BUILD_EXECUTABLE.bat` now uses the versioned spec; removed hardcoded Python path from `run_gui.bat`
- Obsolete spec files archived to `specs/`; `freed_forwarder_config.json` removed from git tracking

---

### v1.9.1 — 2026-05-12

**Bug fixes**

- **Jitter measurement accuracy** — packet timestamps now captured immediately at `recvfrom()` (before any processing); receive thread priority raised to eliminate OS scheduling noise
- **Control bar layout** — wider port/rate spinboxes and extra padding prevent field clipping at all window sizes; uniform field widths (IP 160 px, port/rate 120 px)

**New features**

- **OpenTrackIO Simulator** — standalone `opentrackio_simulator.py` sends synthetic OpenTrackIO JSON over UDP for pipeline testing without a camera
- **Live timecode readout in Timing tab** — Settings → Timecode sub-tab now shows the current running H:M:S:F so you can verify the source without switching tabs
- **Timecode simplified** — system clock is the sole source (manual spinbox input removed); Bluefish444 LTC remains available as an optional external source
- **Send rate auto-sync** — the forwarder send rate automatically follows the configured timecode FPS; no separate rate field needed
- **Genlock-aware jitter health** — the Jitter health banner now factors in genlock lock state when determining the overall signal quality rating
- **F-stop / T-stop sliders in Lens tab** — interactive sliders for f-stop and t-stop values alongside the numeric readout
- New PyInstaller spec files: `FreeD_Reader_V1.0.spec` and `FreeD_Reader_V1.9.1.spec`

---

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
