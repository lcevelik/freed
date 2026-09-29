## Goals

- [x] Release FreeD Dashboard with OpenTrackIO v1.0.1 support — shipped in v3.0.0 on 2026-09-29 (input + output)
- [ ] Add multi-camera tracking support and comparison views — 2026-08-01
- [ ] Publish cross-platform builds (Linux/macOS) — 2026-09-01

## In Progress

- [ ] Validate OpenTrackIO v1.0.1 JSON output against external receivers
- [ ] Improve genlock health reporting (jitter accuracy part done in v3.0.0)

## To Do

- [ ] Recorder: auto-save each recording, or warn before an unsaved recording is replaced
- [ ] Recorder: play back a recording into the live pipeline / outputs
- [ ] Setting for FreeD input lens units (mm × 1000 vs raw encoder counts) — Ocellus FreeD sends encoder counts, the Dashboard assumes mm
- [ ] Receive two OpenTrackIO sources at once (Both mode is one FreeD + one OpenTrackIO)
- [ ] Shorten the header status line — it overflows the default window in Both mode
- [ ] Add support for tracking multiple FreeD sources simultaneously
- [ ] Add Linux and macOS platform support
- [ ] Create OpenTrackIO output configuration UI for custom field mapping
- [ ] Add network diagnostics tab for UDP packet loss monitoring

## Done

- [x] Real-time FreeD D1 packet reception over UDP with checksum validation
- [x] PyQt6 dark-theme GUI with Dashboard, Packet Map, Jitter, and Settings tabs
- [x] OpenTrackIO v1.0.1 JSON forwarding and simulator for testing
- [x] OpenTrackIO input (JSON + CBOR) with segment reassembly; FreeD / OpenTrackIO / Both / Auto-detect input modes, detected-senders table, sender lock
- [x] OpenTrackIO → FreeD conversion (raw encoder counts, standard checksum) and unchanged OpenTrackIO relay
- [x] Packet Map › OpenTrackIO overview (six cards, fits the default window)
- [x] Recorder tab — record 10–60 s, per-field analysis (Live / Fixed / Zero / Not sent), save / load `.fdrec`, CSV export
- [x] Jitter measurement accuracy — perf_counter timestamps (monotonic had 15.6 ms resolution on Windows)
- [x] Standard FreeD checksum accepted and written (Sony Ocellus); legacy 0xF6 still accepted
- [x] Ocellus vs FR7 OpenTrackIO comparison report (PDF, 2026-09-29)

## Blocked

- Ocellus OpenTrackIO lens data — distortion all zero, entrance pupil zero, lens make/model "N/A", focal length in whole-mm steps. Waiting on Sony / camera lens-metadata setup (report sent 2026-09-29). The FR7 sends lens identity and entrance pupil but also zero distortion.
- Sony FE PZ 28-135 on the Ocellus camera reported frozen lens values (135 mm, focus 4.095e10 m) — recheck with the lens confirmed moving.

## Releases

- v3.0.0 — current (2026-09-29) — OpenTrackIO input, Recorder & analyzer, CBOR, FreeD checksum fix — https://github.com/lcevelik/freed/releases/tag/v3.0.0
- v2.0.0 — 2026-05-12 — project restructure, OpenTrackIO output and simulator
- Next — not scheduled — multi-camera support and cross-platform builds

## Notes

- FreeD checksum: standard `(0x40 - sum of bytes 0–27) & 0xFF` verified on Sony Ocellus (722/722 packets); the older device's `(byte26 + byte27 + byte28) & 0xFF == 0xF6` is still accepted on input
- Studio streams (2026-09-29): Ocellus OpenTrackIO → port 5000, Ocellus FreeD → port 40000, FR7 OpenTrackIO → port 6000 (JSON or CBOR)
- Settings persist in %APPDATA%\FreeDReader\; recordings default to Documents\FreeD Recordings; standalone .exe via PyInstaller (no Python required)
- GitHub CLI installed and signed in (lcevelik); in terminals opened before the install use "C:\Program Files\GitHub CLI\gh.exe"
