# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**FreeD Dashboard** (`freed_reader.py`) is a PyQt6 dark-theme GUI that receives,
parses, and analyses camera tracking data over UDP — FreeD D1 and OpenTrackIO
(e.g. Sony Ocellus) input.

Current version: **v2.0.0**  
Author: Libor Cevelik  
Platform: Windows  
Python: 3.8+

---

## Key Files

| File | Purpose |
|------|---------|
| `freed_reader.py` | Main entry point — GUI (`FreeDDashboard`), input routing, CLI `main()` |
| `src/protocol.py` | `FreeDParser`, `FreeDReceiver` (CLI loop), `FreeDReceiverGUI` (per-protocol state), `UdpListener` + `detect_protocol` (GUI sockets) |
| `src/opentrackio.py` | `OpenTrackIOSender` (output + relay), `OpenTrackIOParser` (input reassembly), `oti_to_freed_packet` |
| `src/forwarder.py` | `FreeDForwarder` — destinations, TC injection, **all config persistence** (incl. input mode/ports) |
| `src/ltc_reader.py` | `BluefishLTCReader` (ctypes wrapper for the Bluefish444 DLL) |
| `src/ui_utils.py` | Platform fonts, `configure_stdout()` (devnull redirect for `--noconsole`) |
| `simulators/freed_simulator.py` | Sends synthetic 29-byte FreeD D1 UDP packets for testing |
| `simulators/opentrackio_simulator.py` | Sends synthetic OpenTrackIO JSON UDP packets for pipeline testing |
| `tests/test_freed.py` | FreeD parser / forwarder / OTI output tests |
| `tests/test_opentrackio_input.py` | OTI input: header, reassembly, conversion, `UdpListener` over loopback |
| `tests/data/ocellus_sample.json` | Real captured Ocellus sample used as a fixture |
| `specs/FreeD_Reader_V2.0.0.spec` | Current PyInstaller build spec (older specs in `specs/` are historical) |

---

## Architecture

```
UdpListener per port (src/protocol.py) — recvfrom(65535), perf_counter timestamp,
       │  detect_protocol(): 0xD1 → 'freed', 'OTrk' → 'oti'; sender table; ip_filter
       │  wrong protocol for the port → listener.mismatch (UI offers to switch)
       │
       ├─ freed ─► FreeDDashboard._on_freed_datagram
       │             self.receiver (FreeDReceiverGUI state).display_data()
       │             if active: FreeDForwarder.forward() (+TC inject) ; OpenTrackIOSender.send()
       │
       └─ oti ───► FreeDDashboard._on_oti_datagram
                     if active: OpenTrackIOSender.relay(raw datagram)
                     OpenTrackIOParser.feed() → sample (after last segment)
                     oti_to_freed_packet() → self.oti_state.display_data()   (sample in data['oti'])
                     if active: FreeDForwarder.forward(pkt, inject_tc=False)

QTimer 100 ms → _update() reads _view_state() (receiver or oti_state, per _active_source)
             → Dashboard / Packet Map / Jitter unchanged; OpenTrackIO tab + Network page at 2 Hz
```

Input modes (`FreeDForwarder.INPUT_MODES`): `freed`, `oti`, `both` (active source picked in the
header), `auto` (one port, active source follows traffic). `_listen_plan()` maps port → accepted
protocols; equal ports in `both` share one socket. OpenTrackIO input is converted to FreeD form
internally so the existing Dashboard/Jitter code works on it unchanged.

---

## FreeD D1 Packet — 29 bytes

| Bytes | Field | Scale | Unit |
|-------|-------|-------|------|
| 0 | Message type | — | 0xD1 |
| 1 | Camera ID | — | integer |
| 2–4 | Pan | ÷ 32768 | degrees |
| 5–7 | Tilt | ÷ 32768 | degrees |
| 8–10 | Roll | ÷ 32768 | degrees |
| 11–13 | X | ÷ 64 ÷ 1000 | meters |
| 14–16 | Y | ÷ 64 ÷ 1000 | meters |
| 17–19 | Z | ÷ 64 ÷ 1000 | meters |
| 20–22 | Zoom | ÷ 1000 | mm |
| 23–25 | Focus | ÷ 1000 | meters |
| 26–27 | Spare / Genlock | upper nibble = phase | timecode / genlock |
| 28 | Checksum | `(b26 + b27 + b28) & 0xFF == 0xF6` | — |

**Checksum formula (device-verified):** `(byte26 + byte27 + byte28) & 0xFF == 0xF6`
This is NOT a standard XOR. Determined by live capture across 200+ packets.

Zoom/focus `÷ 1000` is the simple display path. The dashboard's OTI output and the
CLI instead use piecewise-linear interpolation over `zoom_calibration` / `focus_calibration`
in `FreeDReceiver` (`src/protocol.py`). Those tables are hardcoded for a Fujinon Premista 28–100
and clamp values outside their range.

The checksum formula lives in three places: `FreeDParser.calculate_checksum`, `FreeDForwarder`
TC injection, and `freed_simulator.build_freed_packet`. Keep all three in sync.

Optional 4-byte extension (bytes 29–32): H, M, S, F timecode block injected by the forwarder.

---

## Timecode

- Source: **system clock** by default; optional **Bluefish444 LTC** via ctypes DLL
- `BluefishLTCReader` wraps `BlueVelvetC64.dll` — gracefully unavailable if DLL/card missing
- TC frame count derived from `microsecond / 1_000_000 * fps_int`
- Forwarder injects TC into bytes 26–27 (2-second resolution spare field) and appends bytes 29–32 (full H:M:S:F)
- **Send rate automatically follows `tc_fps`** — no separate rate field

---

## Config Persistence

Stored in `%APPDATA%\FreeDReader\freed_forwarder_config.json`:

```json
{
  "destinations": [...],
  "tc_inject": true,
  "tc_source": "system",
  "tc_fps": 25.0,
  "ltc_connector": 2,
  "oti_enabled": false,
  "oti_ip": "127.0.0.1",
  "oti_port": 55555,
  "oti_subject": "Camera",
  "oti_source_id": "<uuid>",
  "listen_port": 45000,
  "input_mode": "freed",
  "oti_listen_port": 45000,
  "active_source": "freed",
  "freed_sender_ip": "",
  "oti_sender_ip": ""
}
```

---

## UI Tabs

| Tab | Sub-tabs | Content |
|-----|----------|---------|
| Dashboard | — | Rotation, Position, Lens, Genlock, Status (timecode + packets), Raw Packet |
| Packet Map | — | Byte-by-byte table: hex / field / raw / decoded |
| Jitter | Monitor, Reference | Timing stats, Position noise (X/Y/Z), Rotation noise (Pan/Tilt/Roll), genlock-aware health banner |
| OpenTrackIO | — | Device, transform, lens, distortion, timing, stream stats, raw JSON of the last OTI sample |
| Settings | Network, Output, Timecode | Input mode/ports, sender lock, detected senders; forwarding destinations, OpenTrackIO; TC source/FPS/connector |

---

## Jitter Health Thresholds

| Metric | IDEAL | ACCEPTABLE | MARGINAL | PROBLEMATIC |
|--------|-------|-----------|----------|-------------|
| Timing | < 1 ms | < 3 ms | < 5 ms | ≥ 5 ms |
| Position noise | < 0.1 mm | < 0.5 mm | < 1.0 mm | ≥ 1.0 mm |
| Rotation noise | < 0.01° | < 0.05° | < 0.10° | ≥ 0.10° |

Genlock lock state is also factored into the overall health banner rating.

---

## Run

```bash
python freed_reader.py                    # GUI (default)
python freed_reader.py --cli --port 45000 # headless console reader (argparse in main())
python simulators/freed_simulator.py        # GUI that sends synthetic FreeD packets
python simulators/opentrackio_simulator.py  # GUI that sends synthetic OpenTrackIO packets
```

`scripts/*.bat` are launch/build shortcuts; they `cd` to the repo root first.

---

## Build

```bash
pip install PyQt6 numpy pyinstaller
pyinstaller specs/FreeD_Reader_V2.0.0.spec      # or scripts\BUILD_EXECUTABLE.bat
# Output: dist\FreeD_Reader_V2.0.0.exe
```

A version bump means updating `__version__` in `freed_reader.py`, creating a new spec in `specs/`,
pointing `scripts/BUILD_EXECUTABLE.bat` at it, and updating the version references in README.md
and this file.

---

## Tests

```bash
pip install pytest
pytest tests/                                              # all tests
pytest tests/test_freed.py::TestFreeDParser                # one class
pytest tests/test_freed.py::TestFreeDParser::test_parse_valid_packet   # one test
```

The tests import `src.protocol` / `src.opentrackio` directly and load `freed_reader` via
`_import_freed_reader()`, which stubs PyQt6 and numpy. The packet-building
helpers at the top of `tests/test_freed.py` are **copies** of the `freed_simulator` functions. If you
change the packet layout in the simulator, update those copies too. There is no linter configured.

---

## Coding Conventions

- **No f-string walrus / match** — Python 3.8 compatibility required
- Dark theme colours defined as class constants on `FreeDDashboard` (BG, CARD, FG, GREEN, CYAN, YELLOW, ORANGE, RED, DIM)
- Font selection is platform-aware (`FONT_MONO`, `FONT_SANS` in `src/ui_utils.py`)
- All UI widgets use `background: transparent` style to inherit card background
- Thread safety: `FreeDForwarder._lock` guards `destinations` list; `BluefishLTCReader._lock` guards TC values
- `QTimer(100ms)` drives all UI updates from the main thread — never touch widgets from a `UdpListener` thread
- Config saved on every meaningful UI change (not just on exit)

---

## Common Gotchas

- `timeBeginPeriod(1)` must be called before the receive loop starts — already done at module import in `freed_reader.py` on Windows
- `recvfrom` timestamp must be captured **before** any parsing — jitter is measured at socket level
- Use `time.perf_counter()` for packet timestamps, never `time.monotonic()` — on Windows with Python < 3.13 monotonic ticks every 15.6 ms and fakes ~8 ms of jitter. All receive-path timestamps must use the same clock
- OpenTrackIO senders segment large messages (Ocellus: 1216 + ~285 byte datagrams per sample) — never read with a small `recvfrom` buffer
- OTI → FreeD packets are sent **without** TC (`forward(..., inject_tc=False)`); TC injection is for FreeD input only
- The OTI relay is suppressed (`_relay_blocked`) when the OTI output points at this machine's own OTI listen port
- Bluefish DLL path is hardcoded to `C:\Program Files\Bluefish444\...` — `available=False` if missing, all code falls back to system clock silently
- The permanent forwarding destination `127.0.0.1:40000` is always prepended and never saved (reconstructed on load)
- PyInstaller `--noconsole` mode sets `stdout/stderr` to `None` — the module-level guard redirects them to `devnull` (`src/ui_utils.configure_stdout()`, also used by both simulators)
