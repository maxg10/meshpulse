# Changelog

## v2.4.8-stable
- Fix: node counter (#cnt) now correctly respects "Show only direct"
  and "Hide unknown hops" filters for the no-GPS nodes section.
  Previously filteredNoGps was filtered after the counter was already
  rendered, causing the count to be inflated (e.g. "220 (11+209)"
  instead of "12 (11+1)" in direct-only mode).

## v2.4.7-stable
- External Notification: added 8 missing fields (alert_message_buzzer,
  alert_message_vibra, alert_bell_buzzer, alert_bell_vibra,
  use_i2s_as_buzzer, output, output_buzzer, output_vibra) and
  regrouped UI into General / Triggers / Output Configuration
  sub-sections with English tooltips. Existing legacy fields kept
  with "(legacy)" labels for compatibility.
- Fix: needsReboot logic now covers all module configs (not just
  lora/device). Module configs only load at boot, so changes to
  any module now correctly prompt for reboot. Affected sections:
  mqtt, serial_module, ext_notification, store_forward, range_test,
  canned_message, paxcounter, audio, neighbor_info, detection_sensor,
  ambient_lighting, remote_hardware, network, bluetooth, security,
  power.

## v2.4.6-stable
- Fix: Plugin re-enabled automatically after update — was getting
  stuck as disabled because install() called disable() but never
  re-enabled after extracting the new version
- Fix: "Installing plugin... up to 60s" message now persists until
  install actually finishes — was disappearing after 5s due to
  generic showAlert auto-dismiss timeout, leaving user staring at
  empty status bar during the actual install

## v2.4.5
- Fix: update tracker name in nodes[] when long_name changes

## v2.4.4
- Fix: plugin send_mesh_message uses get_running_loop() for correct event loop
- Fix: plugin serial send wrapped in asyncio.wait_for with timeout and logging
- Fix: plugin._mapper updated on mapper restart
- Fix: CLI config commands use actual detected serial port (not hardcoded ttyACM0)
- Fix: neighborinfo saved via Python API instead of CLI
- Fix: [USB] label for serial packets instead of [TCP]
- Fix: pre-select DM recipient when clicking Send Message on map
- Fix: ROUTER_CLIENT and CLIENT_HIDDEN in device role dropdown
- Feature: plugin documentation link on plugin cards
- Feature: auto-check plugin updates on startup with badge and toast notification
- Feature: progress feedback during plugin install/enable
- UI: Relay Activity card above hourly charts

## v2.4.3
- Fix: Relayed (24h) card now shows real relay count from firmware numTxRelay delta (radio_stats_history) — replaces always-zero Python API detection
- Feature: New Relay Activity (24h) panel — total relayed, avg/hour, avg/min, peak hour, hourly bar chart
- Remove: Relay Flow section — replaced by Relay Activity with real data
- Feature: Auto-check for plugin updates on startup (60s delay) — shows badge and toast notification when updates available

## v2.4.2
- Fix: Relayed (24h) now uses firmware numTxRelay delta from radio_stats_history — accurate relay count instead of always-zero Python API detection
- Remove: TX Relay Trend chart — replaced by TX Relay delta widget in Radio Stats History
- Cleanup: removed dead relayed_nodes and topology queries from backend stats

## v2.4.0
- Fix: `showAlert` undefined in `stats.html` ws.onmessage — crashed Full Reset handler, preventing NoGPS nodes from being cleared (fix was in dev, now released)
- Fix: `save_nodes_json()` → `save_nodes()` typo in backend full_reset handler (fix was in dev, now released)
- Feature: Live Network Monitor panel in Stats — real-time Packets/min (60s sliding window), Active Nodes (15min window), Live SNR/RSSI scrolling chart, Channel Pulse widget
- Feature: TX Relay Trend chart (24h) replacing empty "Relayed Through Your Node" table — shows numTxRelay history from localStats snapshots
- Backend: New `radio_stats_history` SQLite table — stores every localStats snapshot with timestamp, 7-day retention
- Backend: New `packet_event` WebSocket message — lightweight per-packet broadcast enabling real-time frontend widgets

## v2.3.0-stable
- Feature: MQTT Proxy plugin — MQTT client proxy for trackers without WiFi (uplink, downlink, implicit ACK)
- Feature: Plugin Store — browse and install plugins from meshtastic.world/plugins directly in Config → Plugins
- Feature: Plugin update detection — shows "Update" button when newer version available in store
- Feature: Plugin auto-dependency install — pip install requirements.txt on plugin enable
- Feature: Backup & Restore — quick (config + plugin settings) and full (+ nodes + stats) with ZIP download/upload
- Feature: Checkbox state persistence — all Mesh Info filter checkboxes saved to localStorage
- Feature: Elevation Map plugin checkbox state persistence across page refresh
- Feature: Version single source of truth — frontend reads version from backend via WebSocket, no hardcoded strings
- Feature: Plugin Store page at meshtastic.world/plugins
- Feature: Separate plugin repositories — plugins distributed via GitHub releases
- Improvement: Plugin architecture docs updated — on_mqtt_proxy hook, API methods table, MQTT proxy section
- Improvement: Added missing telemetry fields — numTxRelayCanceled, heapFreeBytes, heapTotalBytes
- Improvement: Docker image updated with plugin system support (mapper/ module)
- Fix: protobuf MessageToDict compatibility (always_print_fields_with_no_presence / including_default_value_fields)
- Fix: protobuf camelCase field names (mqttClientProxyMessage)
- Fix: pubsub mqttclientproxymessage listener signature

## v2.2.0
- Feature: Plugin system — install, enable, disable, uninstall plugins from web UI
- Feature: Plugin API — MeshPlugin base class with 11 hooks (on_message, on_node_update, on_position, on_telemetry, on_neighborinfo, on_connect, on_disconnect, on_node_expire, on_ws_client_connect, on_ws_client_disconnect)
- Feature: Frontend Plugin API — MapperAPI proxy with map, nodes, messages, WebSocket, UI, and storage access
- Feature: Plugins tab in Config — manage installed plugins, upload .meshplugin files
- Feature: Plugin namespace isolation — each plugin's layers, controls, storage are automatically prefixed
- Feature: Plugin config persistence — auto-generated settings UI from plugin manifest
- Feature: Clear All Statistics button on Stats page (stats only or full reset with nodes)

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
