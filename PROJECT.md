## Goals

- [ ] Release FreeD Dashboard v2.0 with OpenTrackIO v1.0.1 support — 2026-06-15
- [ ] Add multi-camera tracking support and comparison views — 2026-08-01
- [ ] Publish cross-platform builds (Linux/macOS) — 2026-09-01

## In Progress

- [ ] Validate OpenTrackIO v1.0.1 JSON output against external receivers
- [ ] Improve jitter analysis accuracy and genlock health reporting

## To Do

- [ ] Add support for tracking multiple FreeD sources simultaneously
- [ ] Implement data recording and playback (export to CSV/JSON)
- [ ] Add Linux and macOS platform support
- [ ] Create OpenTrackIO output configuration UI for custom field mapping
- [ ] Add network diagnostics tab for UDP packet loss monitoring

## Done

- [x] Real-time FreeD D1 packet reception over UDP with checksum validation
- [x] PyQt6 dark-theme GUI with Dashboard, Packet Map, Jitter, and Settings tabs
- [x] OpenTrackIO v1.0.1 JSON forwarding and simulator for testing

## Blocked



## Releases

- v2.0.0 — current — FreeD Dashboard with OpenTrackIO output and simulator
- v2.1.0 — planned 2026-09-01 — Multi-camera support and cross-platform builds

## Notes

- Checksum formula verified with real hardware: (byte26 + byte27 + byte28) & 0xFF == 0xF6
- Settings persist in %APPDATA%\FreeDReader\; standalone .exe via PyInstaller (no Python required)
