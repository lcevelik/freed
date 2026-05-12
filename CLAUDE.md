# CLAUDE.md — FreeD Dashboard Project Guide

Project context for AI coding assistants.

---

## Project Overview

**FreeD Dashboard** (`freed_reader.py`) is a PyQt6 dark-theme GUI that receives,
parses, and analyses camera tracking data from the FreeD D1 protocol over UDP.

Current version: **v1.9.1**  
Author: Libor Cevelik  
Platform: Windows  
Python: 3.8+

---

## Key Files

| File | Purpose |
|------|---------|
| `freed_reader.py` | Main entry point — GUI (`FreeDDashboard`), forwarder (`FreeDForwarder`), Bluefish LTC reader (`BluefishLTCReader`) |
| `protocol.py` | `FreeDParser`, `FreeDReceiver`, `FreeDReceiverGUI` — packet parsing and UDP receive loop |
| `opentrackio.py` | `OpenTrackIOSender` — emits OpenTrackIO v1.0.1 JSON over UDP |
| `freed_simulator.py` | Sends synthetic 29-byte FreeD D1 UDP packets for testing |
| `opentrackio_simulator.py` | Sends synthetic OpenTrackIO JSON UDP packets for pipeline testing |
| `tests/test_freed.py` | 48 pytest unit tests |
| `FreeD_Reader_V1.9.1.spec` | PyInstaller build spec for the standalone EXE |

---

## Architecture

```
UDP socket (recvfrom)
       │
FreeDReceiver (protocol.py)
       │  raw bytes
       ├─► FreeDParser.parse()  →  dict with camera data
       │
FreeDDashboard (freed_reader.py)  ←  QTimer 100ms
       │
       ├─► UI update (Dashboard, Packet Map, Jitter tabs)
       ├─► FreeDForwarder.forward()  →  TC injection → UDP destinations
       └─► OpenTrackIOSender.send()  →  JSON UDP
```

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
  "listen_port": 45000
}
```

---

## UI Tabs

| Tab | Sub-tabs | Content |
|-----|----------|---------|
| Dashboard | — | Rotation, Position, Lens, Genlock, Status (timecode + packets), Raw Packet |
| Packet Map | — | Byte-by-byte table: hex / field / raw / decoded |
| Jitter | Monitor, Reference | Timing stats, Position noise (X/Y/Z), Rotation noise (Pan/Tilt/Roll), genlock-aware health banner |
| Settings | Network, Output, Timecode | Port, forwarding destinations, OpenTrackIO, TC source/FPS/connector |

---

## Jitter Health Thresholds

| Metric | IDEAL | ACCEPTABLE | MARGINAL | PROBLEMATIC |
|--------|-------|-----------|----------|-------------|
| Timing | < 1 ms | < 3 ms | < 5 ms | ≥ 5 ms |
| Position noise | < 0.1 mm | < 0.5 mm | < 1.0 mm | ≥ 1.0 mm |
| Rotation noise | < 0.01° | < 0.05° | < 0.10° | ≥ 0.10° |

Genlock lock state is also factored into the overall health banner rating.

---

## Build

```bash
pip install PyQt6 numpy pyqtgraph pyinstaller
pyinstaller FreeD_Reader_V1.9.1.spec
# Output: dist\FreeDReader_v1.9.1.exe
```

---

## Tests

```bash
pip install pytest
pytest tests/
# 48 tests covering parser, checksum, interpolation, TC injection, OTI output
```

---

## Coding Conventions

- **No f-string walrus / match** — Python 3.8 compatibility required
- Dark theme colours defined as class constants on `FreeDDashboard` (BG, CARD, FG, GREEN, CYAN, YELLOW, ORANGE, RED, DIM)
- Font selection is platform-aware (`_FONT_MONO`, `_FONT_SANS` set at module level)
- All UI widgets use `background: transparent` style to inherit card background
- Thread safety: `FreeDForwarder._lock` guards `destinations` list; `BluefishLTCReader._lock` guards TC values
- `QTimer(100ms)` drives all UI updates from the main thread — never touch widgets from `recv_thread`
- Config saved on every meaningful UI change (not just on exit)

---

## Common Gotchas

- `timeBeginPeriod(1)` must be called before the receive loop starts — already done in `__init__` on Windows
- `recvfrom` timestamp must be captured **before** any parsing — jitter is measured at socket level
- Bluefish DLL path is hardcoded to `C:\Program Files\Bluefish444\...` — `available=False` if missing, all code falls back to system clock silently
- The permanent forwarding destination `127.0.0.1:40000` is always prepended and never saved (reconstructed on load)
- PyInstaller `--noconsole` mode sets `stdout/stderr` to `None` — the module-level guard redirects them to `devnull`
