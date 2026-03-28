# Changelog

## v2.0.8
- Power/MQTT/Display/Modules config tabs in config.html
- Device disconnect indicator (yellow "reconnecting" status)
- Fix: config checkboxes no longer reset after save (AttributeError on missing firmware fields)
- All config fields use `getattr(obj, field, default)` — safe across firmware versions
- Removed automatic `get_config` reload after save

## v2.0.7
- XSS fix: `escHtml()` in all frontend files
- Security: `sanitize_str/message/node_id()` for all radio input, `safe_json()` + 64KB WS limit
- Auto-update check via GitHub API (once per session)
- Stats WS message size reduced ~120KB → ~15KB
- Fix: topology zoom/pan, visibility handler, WS handshake log noise

## v2.0
- `frontend/config.html` — full device config (Device/LoRa/Position/Telemetry/Network/BT/Channels/Favorites)
- `frontend/messages.html` — standalone messages page
- Full channel editor with PSK management
- `TCPMeshtasticInterface` proxy class
- Unread badge on Messages nav via `BroadcastChannel`
- Three-layer node dedup (`_dedup_nodes()`)

## v1.19
- Serial uses Python Meshtastic API (`SerialMeshtasticInterface`) — no more subprocess
- `_run_serial()`, `_on_serial_packet()`, `_handle_traceroute_packet()`
- Traceroute works without pausing listener (serial + TCP)
- `Via` column in Most Active Nodes
- DM button in Messages + No-GPS panel, TR button in No-GPS panel

## v1.16
- `TCPMeshtasticInterface` class wrapping `meshtastic.tcp_interface.TCPInterface`
- `_run_tcp()` + `_on_tcp_packet()` — TCP listener via Python API (no CLI subprocess)
- Packet parsers: `parse_node_info/position/telemetry/text_from_packet()`

## v1.13
- Traceroute: `run_traceroute()`, `parse_traceroute_output()`, serial/TCP handling
- Earth curvature correction in `checkLOS()` (`R_eff = 8500000m`)
- `source` field on nodes: `'memory'`/`'live'`
- Default TTL changed: 7 days → 48 hours

## v1.12
- TCP connection support (`--host` flag), runtime USB↔TCP switching
- Config persistence (`config.json`), `connection_status` WS message
- Radio Stats panel with localStats telemetry

## v1.11
- LOS panel with terrain profile, Chart.js, Open-Elevation API
- `checkLOS()` with Fresnel zone (868MHz), `fresnelRadius()`, `interpolatePoints()`

## v1.10
- Own tracker blue marker, distance in popup, role field, "Hide unknown hops" filter

## v1.9 — v1.8
- Safari detection + WS delays
- Direct connection lines (SNR color-coded), heat map (leaflet.heat)
