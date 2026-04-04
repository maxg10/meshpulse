# Changelog

## v2.1.1-stable
- Feature: Anonymous telemetry — opt-out daily ping with version, platform, OS, arch, uptime. No personal data collected. Disable in Config → Coverage tab
- Feature: Export nodes to CSV — download all nodes as CSV file from Stats page
- Feature: Sortable columns in Most Active Nodes table on Stats page
- Feature: Bad packets percentage displayed next to Avg SNR in Mesh Info panel (shows when radio stats data is available)
- Feature: Tooltip on Antenna Alt field explaining height Above Ground Level
- Fix: Coverage simulation time hint changed from ~30s to ~60s
- Meta: Privacy policy updated on meshtastic.world to document telemetry data collection
- Meta: Telemetry admin dashboard at meshtastic.world/mcsadmin.html

## v2.1.0-stable
- Feature: RF Coverage overlay — terrain-aware propagation map overlaid directly on the Leaflet map via Coverage Server API (coverage.meshtastic.world). Plasma colormap, opacity slider, coverage legend
- Feature: Coverage tab in Config — server URL, API key, antenna gain, height AGL, test connection button
- Feature: Ignored Nodes — manage ignored/blocked nodes in Config → Device tab. Set/remove ignored nodes, see current ignore list
- Feature: Mobile responsive UI — bottom drawer panels for MeshInfo and NoGPS, floating action buttons (FAB), fullscreen map, two-row navbar in portrait mode
- Feature: PWA support — manifest.json, service worker with static asset caching, app icons, "Install as App" banner on mobile
- Feature: update.sh — one-command update script (git pull + install + restart)
- Feature: Radio Health memory — stats remember last non-zero values instead of resetting to 0/0, shows "updated Xm ago" timestamp
- Feature: Project website at meshtastic.world
- Fix: Tracker marker always rendered on top of other node markers (custom Leaflet pane with higher z-index)
- License: Changed from MIT to GPL-3.0, added GPL headers to all source files, added GitHub Sponsors funding config
- Meta: Added .github/FUNDING.yml for GitHub Sponsors

## v2.0.11
- Feature: Radio vs MQTT packet breakdown in Stats — Packets (24h) card now shows split between radio and MQTT received packets with percentages
- Feature: okay_to_mqtt checkbox in Config → MQTT tab — controls whether your node packets can be forwarded to MQTT by other nodes
- Fix: Docker image now supports Apple Silicon (arm64) — multi-platform build (linux/amd64 + linux/arm64)
- Fix: Docker frontend files always updated on container restart — fresh HTML/CSS copied from image on startup (fixes stale version after docker compose pull)

## v2.0.10
- Fix: Docker volume no longer overwrites frontend files on container update — fresh HTML/CSS copied from image on every startup
- Fix: entrypoint.sh version string updated

## v2.0.9
- Fix: WebSocket JSON parse errors no longer crash message handlers in config.html, stats.html, messages.html (try/catch added, matching index.html pattern)
- Fix: Device disconnect banner in config.html now clears automatically when device reconnects
- Fix: MapReportSettings field name corrected: `publish_secs` → `publish_interval_secs` (fixes MQTT Map Reporting save error)
- Fix: Map Reporting publish interval minimum set to 3600s (firmware enforced minimum)
- Fix: MQTT config persistence after device reboot — added write delay before reboot, config reloads automatically after reconnect
- Fix: Relay node name collision in Stats "Via" column — last-byte relay IDs (≤0xFF) no longer falsely resolved to wrong node names
- Fix: Config page shows yellow "Device disconnected" banner during serial reconnect cycle
- Fix: showAlert() auto-dismisses success/info alerts after 5s; warning/error alerts persist until resolved
- Feature: MQTT Uplink/Downlink toggles per channel in Config → Channels tab
- Feature: Config → MQTT tab now includes `okay_to_mqtt` field

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
