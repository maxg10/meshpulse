# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Meshtastic Network Mapper is a real-time web visualization tool for Meshtastic mesh network nodes. It connects to a Meshtastic device via USB serial or TCP, parses node/position/telemetry packets from `meshtastic --listen`, and displays nodes on an interactive Leaflet.js map with WebSocket support for real-time updates. Optimized for low-power devices (Raspberry Pi Model B+, 512MB RAM).

**Current Version:** v1.20 (Standalone Messages Page)

## Running & Deployment

**Run backend manually:**
```bash
python3 backend/meshtastic_mapper.py
```

**Install as systemd service:**
```bash
./install.sh
```

**Service management:**
```bash
sudo systemctl start meshtastic-mapper
sudo systemctl status meshtastic-mapper
sudo journalctl -u meshtastic-mapper -f
```

The installer copies frontend files to `/var/www/html/meshtastic/` and generates a systemd service file from `systemd/meshtastic-mapper.service.template`. Web access at `http://<host>/meshtastic/`, WebSocket at `ws://<host>:8765`.

There is no build step, test suite, or linter configured for this project.

## Architecture

### Backend (`backend/meshtastic_mapper.py`)

Single Python file (~1225 lines) with the `ListenBasedMapper` class:

- **Subprocess model**: Spawns `meshtastic --listen` as a subprocess, parses stdout line-by-line in real-time. Auto-restarts on process termination.
- **Four parsers**: `parse_node_info()`, `parse_position_update()`, `parse_telemetry_update()`, `parse_text_message()` — each handles a different packet type from the meshtastic CLI output.
- **Dual data stores**: `self.nodes` (nodes with GPS positions) and `self.nodes_no_position` (nodes without GPS). Both are dicts keyed by node ID (e.g., `!7b6c8272`).
- **Messages storage**: `self.messages` list stores up to 50 recent text messages (broadcasts and DMs), newest first. Persisted to `nodes.json` and reloaded on startup.
- **Source tracking**: Every node has a `source` field — `'memory'` when loaded from `nodes.json` at startup, `'live'` once a real packet is received. Frontend shows "from memory" indicator for memory-sourced nodes.
- **JSON output**: Writes `nodes.json` every 60 seconds to the web server directory. Includes nodes, no-GPS nodes, messages, and tracker info. Stale nodes cleaned every hour based on `max_age` (default 48 hours / 172800 seconds).
- **WebSocket server**: Async server on port 8765 running in a separate thread via `threading.Thread` + `asyncio`. Broadcasts message types to all connected clients:
  - `node_update` - real-time node data updates
  - `node_deleted` - when old nodes are removed
  - `new_message` - text messages (broadcasts and DMs)
  - `stats_update` - max distance / farthest node
  - `connection_status` - tracker connection state + tracker info
  - `traceroute_status` - traceroute progress (starting/reconnecting)
  - `traceroute_result` - traceroute hop data + raw output
- **Serial port detection**: Auto-detects USB serial port from list of common ports (`/dev/ttyUSB[0-2]`, `/dev/ttyACM[0-2]`)
- **Tracker info**: Extracts local node ID, hardware model, firmware version, and uptime via `meshtastic --info`
- **Distance**: Haversine formula in `calculate_distance()` for km distances; `get_max_distance()` finds the farthest directly-reachable node (hops=0).
- **TCP support**: Runtime switching between USB serial and TCP (`--host`) via web UI. Config persisted to `/var/www/html/meshtastic/config.json`.
- **Runtime restart**: `restart_event` (threading.Event) + `restart_config` dict coordinate connection changes between WebSocket thread and main loop. `traceroute_restart` flag distinguishes traceroute-triggered restarts (preserve nodes.json) from connection-change restarts (may clear nodes.json).

### Traceroute Feature

- **Backend**: `run_traceroute(node_id, websocket)` async coroutine in module scope. Handles two modes:
  - **TCP mode**: Runs `meshtastic --host <host> --traceroute <node_id>` in asyncio executor alongside the running listener (no interruption). Timeout: 60s.
  - **Serial/USB mode**: Terminates `mapper.current_process` (listener), waits 2s for port release, runs traceroute, waits 3s after completion, then sets `traceroute_restart = True` + `restart_event.set()` to restart listener without clearing nodes.json.
- **Parser**: `parse_traceroute_output(output)` handles variable meshtastic CLI output formats. Splits on `-->`, extracts hop names and SNR values via regex. Enriches hops with coordinates from `mapper.nodes`.
- **WebSocket flow**: `traceroute_status {starting, connection_type}` → subprocess runs → `traceroute_status {reconnecting}` (serial only) → `traceroute_result {route, route_back, raw}`.
- **Frontend**: `startTraceroute(nodeId)` shows confirm dialog for serial mode. Sends `{type: 'traceroute', node_id}`. Panel shows 60s countdown timer. `handleTracerouteResult()` draws SNR-colored polylines (solid=forward, dashed=return path) and enriches hops from `allNodes` as frontend fallback. 90s safety timeout prevents stuck panel.

### Radio Stats Parsing

`parse_telemetry_update()` extracts `localStats` fields from the tracker's own telemetry:
- Fields: `channelUtilization`, `airUtilTx`, `numPacketsTx`, `numPacketsRx`, `numPacketsRxBad`, `numRxDupe`, `numTxRelay`, `numOnlineNodes`, `numTotalNodes`
- Only parsed when `node_id == self.local_node_id`
- Stored in `self.tracker_info['radio_stats']` dict
- Persisted to `nodes.json` under `tracker.radio_stats` and restored on startup
- Frontend Radio panel displays these stats with bad-packet percentage calculation

### RSSI Parsing

- `parse_position_update()`: extracts `rxRssi` from position packets, stores on `self.nodes[node_id]['rssi']`
- `parse_telemetry_update()`: extracts `rxRssi` from telemetry packets, updates `rssi` on nodes in both `self.nodes` and `self.nodes_no_position`
- Frontend popup: shows `RSSI: X dBm` for hops=0 nodes, `RSSI: X dBm (last hop)` for relayed nodes
- Mesh Stats panel shows RSSI of the farthest direct node as "Far signal"

### Frontend (`frontend/index.html` + `frontend/styles.css`)

Vanilla JS single-page app with Leaflet.js v1.9.4:

- **Data fetching**: Primary WebSocket connection (`ws://host:8765`) with automatic fallback to JSON polling every 15 seconds. Exponential backoff retry (max 5 attempts). Auto-reconnects on WebSocket disconnect.
- **Node markers**: Color by age (green <1h, yellow 1-6h, red >6h). Shape by role (square=router, circle=client). Dashed border for relayed nodes (hops>0). Offset logic prevents marker overlap. Blue square = own tracker.
- **UI panels**: All collapsible, positioned on map:
  - **Mesh Stats** (top-right): Node counts, avg SNR, max range, far signal (RSSI), tracker info, filter checkboxes, connection status
  - **No-GPS panel** (top-right, dynamic position below stats): Nodes without GPS
  - **Radio panel** (top-left, fixed): Channel utilization, air TX %, packet counts, bad packets, TX relay, online/total nodes
  - **Messages panel** (bottom-left, ~370px from top): Up to 50 recent text messages
  - **Legend** (bottom-left): Visual reference for marker colors/shapes
  - **LOS panel** (top-left, fixed): RF line of sight chart, shown on hops=0 node click when checkbox enabled
  - **Traceroute panel** (top-left, 220px from left): Traceroute results with countdown timer
- **Status indicator**: Green dot = WebSocket connected (real-time), Yellow blinking = connecting, Yellow = polling fallback, Red = disconnected
- **Cache busting**: No-cache meta tags. CSS: `styles.css?v=1.16`. JS: `const MAPPER_VERSION = '1.16'`.

### LOS Analysis (`checkLOS()`)

- **Earth curvature correction**: `curvatureCorrection = (d1 * d2) / (2 * R_eff)` where `R_eff = 8500000m` (effective Earth radius with k=4/3 tropospheric refraction factor). Applied to each terrain elevation point before LOS/Fresnel check.
- **Fresnel zone**: 60% clearance of first zone at 868MHz. `fresnelRadius(d1, d2, 868)` = `sqrt(wavelength * d1 * d2 / (d1 + d2))`
- **Antenna height**: `max(node.alt, terrain_elev) + 10m` offset at both ends
- **Chart datasets**: Terrain (brown, filled), Obstruction Zone (red transparent, only when obstructed, fill between terrain and LOS), LOS line (green/red dashed, triangle markers at endpoints, borderWidth 3), Fresnel zone (blue dashed)
- **Open-Elevation API**: `https://api.open-elevation.com/api/v1/lookup`, 50 sample points per path

### Data Flow

```
Meshtastic device (USB/TCP) → meshtastic --listen (subprocess) → Python parser
  → nodes dict (source:'live') + messages list
  → nodes.json (every 60s) → frontend polling (fallback)
  → WebSocket broadcast (real-time) → frontend WebSocket
    ├── node_update events (position/telemetry/nodeinfo)
    ├── node_deleted events (TTL cleanup)
    ├── new_message events (text messages)
    ├── stats_update events (max distance)
    ├── connection_status events (tracker info, radio stats)
    ├── traceroute_status events (progress)
    └── traceroute_result events (hop data + map lines)
```

## Key Configuration (in `meshtastic_mapper.py`)

- `self.port`: Serial port (auto-detected from `/dev/ttyUSB[0-2]` or `/dev/ttyACM[0-2]`)
- `self.json_path`: Output location (`/var/www/html/meshtastic/nodes.json`)
- `self.max_age`: Node TTL in seconds (default `172800` = 48 hours, set at line ~1172)
- `self.meshtastic_cmd`: Path to meshtastic CLI (`~/.local/bin/meshtastic`)
- WebSocket port: `8765` (hardcoded in `start_websocket_server()`, line ~1100)
- Save interval: `60` seconds (line ~714)
- Cleanup interval: `3600` seconds / 1 hour (line ~717)
- Max messages: `50` messages stored (line ~402 frontend, backend insert/slice)
- Traceroute timeout: `60s` outer asyncio, `57s` inner subprocess

## Dependencies

**Python:**
- Python 3.7+
- `meshtastic` CLI tool (installed via pip: `pip3 install meshtastic`)
- `websockets` library (installed via pip: `pip3 install websockets`)

**System:**
- lighttpd or apache2 for serving frontend files
- systemd for service management
- USB serial access (user must be in `dialout` group)

**Frontend:**
- Leaflet.js v1.9.4 (loaded from CDN)
- leaflet.heat v0.2.0 (heat map)
- Chart.js (LOS elevation chart)
- No build tools required - pure HTML/CSS/JS

## File Structure

```
meshtastic-network-mapper/
├── backend/
│   └── meshtastic_mapper.py      # Main Python backend (~1225 lines)
├── frontend/
│   ├── index.html                # Main web interface with inline JS
│   ├── stats.html                # Network Statistics page (standalone)
│   ├── styles.css                # Separated CSS styles
│   └── favicon.ico               # Browser icon
├── systemd/
│   └── meshtastic-mapper.service.template  # Systemd service template
├── docs/
│   ├── screenshoot.png           # Screenshot for README
│   └── snapshoot.png             # Additional screenshot
├── install.sh                    # Automated installer script
├── README.md                     # User-facing documentation
├── CLAUDE.md                     # This file - AI context
├── LICENSE                       # MIT License
└── .gitignore                    # Git ignore rules
```

## Recent Changes (v1.19)

**Added:**
- Serial connection now uses Python Meshtastic API (`SerialMeshtasticInterface`) — mirrors TCP implementation, no more subprocess
- `_run_serial()` method — Python API listener for USB serial connections
- `_on_serial_packet()` — routes serial packets to shared parsers (same as TCP)
- `_handle_traceroute_packet()` — handles `TRACEROUTE_APP` packets for both serial and TCP
- `_pending_traceroute_result` — stores traceroute result from callback for async retrieval
- Traceroute now works without pausing the listener (serial and TCP)
- Traceroute result shows full route with intermediate hops and SNR values
- `Via` column in Most Active Nodes — shows which node last relayed each packet
- DM button next to each message in Messages panel
- DM recipient search in send area (autocomplete from known nodes)
- TR (Traceroute) button in No GPS panel nodes
- DM button in No GPS panel nodes
- Arrow indicators for collapsible panels (▼ open, ▶ closed)

**Fixed:**
- Traceroute reads from `decoded.traceroute` field (not `routeDiscovery`) in Python API
- SNR values scaled correctly (divided by 4.0 from Meshtastic integer encoding)
- Race condition in traceroute callback vs waiting loop
- USB pause warning dialog removed (Python API doesn't need listener pause)
- Traceroute timeout message updated (removed reference to listener restart)
- Traceroute panel height increased to show Close button for long routes
- Messages panel send area is now sticky at bottom (moved outside scrollable content)
- DM from Messages panel fixed
- No GPS panel uses fixed positioning, repositioned dynamically below Mesh Info
- Navbar z-index increased to 10000 to prevent Leaflet overlap
- `relay_node_id` now stores full node ID (resolved from last byte matching)
- Stats panel uses `position:fixed` instead of `position:absolute`

**Changed:**
- Version bumped to v1.19
- `styles.css?v=1.19`
- Serial mode no longer uses subprocess or regex parsers
- Watchdog updated to disconnect Python API interface instead of killing subprocess
- `top_senders` query includes `last_relay` subquery for Via column

### StatsDB Architecture
- **DB path**: `/var/www/html/meshtastic/stats.db`
- **Tables**: `packets` (per-packet log), `node_activity` (per-node-per-hour summary), `anomalies` (detected events)
- **Relay detection**: compares `relayNode` field (last byte of relaying node num) against our local node ID's last byte
- **Retention**: cleanup runs on every `save_nodes()` call; removes rows older than 3 days
- **Thread safety**: `threading.Lock()` guards all DB connections

## Previous Changes (v1.16)

**Added:**
- `TCPMeshtasticInterface` class — wraps `meshtastic.tcp_interface.TCPInterface` with `connect(on_receive)`, `disconnect()`, `sendText()` methods
- New imports: `meshtastic`, `meshtastic.tcp_interface`, `mesh_pb2`, `portnums_pb2`
- `ListenBasedMapper._run_tcp()` — runs TCP listener using Python API (no CLI subprocess)
- `ListenBasedMapper._on_tcp_packet(packet)` — routes incoming TCP packets by portnum
- `ListenBasedMapper.parse_node_info_from_packet(packet)` — parses NODEINFO_APP dict from Python API
- `ListenBasedMapper.parse_position_from_packet(packet)` — parses POSITION_APP dict from Python API
- `ListenBasedMapper.parse_telemetry_from_packet(packet)` — parses TELEMETRY_APP dict from Python API
- `ListenBasedMapper.parse_text_from_packet(packet)` — parses TEXT_MESSAGE_APP dict from Python API

**Changed:**
- `ListenBasedMapper.run()`: TCP mode now calls `_run_tcp()` instead of spawning CLI subprocess
- `run_send_message()`: TCP mode creates a dedicated `TCPInterface` for sending only — listener is **not** stopped; no `send_restart` / `restart_event` triggered for TCP
- `run_send_message()` serial error handlers no longer trigger restart for TCP connections
- Version bumped to v1.16 (`MAPPER_VERSION`, `styles.css?v=1.16`, backend comment)

## Previous Changes (v1.13)

**Added:**
- Traceroute feature: `run_traceroute()`, `parse_traceroute_output()` in backend
- Traceroute USB/TCP handling: serial mode pauses listener, TCP runs alongside
- `traceroute_restart` global flag — prevents nodes.json clear on traceroute restart
- `traceroute_status` WebSocket message type (starting/reconnecting)
- `traceroute_result` WebSocket message type (route/route_back/raw)
- Frontend: traceroute panel (`#traceroute-panel`) with countdown timer, SNR-colored map lines
- Frontend: `startTraceroute()`, `closeTraceroutePanel()`, `handleTracerouteResult()`
- Frontend: 90s safety timeout for stuck traceroute panel
- Frontend: USB confirm dialog before traceroute ("listener will pause ~60s")
- Earth curvature correction in `checkLOS()` using `R_eff = 8500000m`
- LOS chart: earth-tone terrain colors, obstruction zone (red fill), thicker LOS line, triangle endpoint markers, brighter Fresnel blue
- `source` field on nodes: `'memory'` on load from JSON, `'live'` on live packet
- Frontend popup: "📋 from memory" indicator with tooltip for memory-sourced nodes
- Friendly traceroute error messages (timeout/no-route/generic)

**Changed:**
- Default TTL: 604800s (7 days) → 172800s (48 hours)
- `checkLOS()` now returns `correctedElevations` (with curvature) alongside other values
- Chart plots `correctedElevations` instead of raw elevations
- Traceroute button shown on all non-tracker, non-MQTT node popups (was hops>0 only)
- Version bumped to v1.13 (`MAPPER_VERSION`, `styles.css?v=1.13`, backend comment)

## Previous Changes (v1.12)

**Added:**
- TCP connection support: `--host` flag for WiFi-connected trackers
- Runtime connection switching via web UI (USB ↔ TCP without restart)
- Config persistence: `config.json` saves connection type and host
- `connection_status` WebSocket message with tracker info on connect
- Radio Stats panel (`#radio-panel`) with localStats from telemetry
- `restart_event` + `restart_config` for runtime mapper restarts
- `handle_connection_change()` WebSocket handler

## Previous Changes (v1.11)

**Added:**
- LOS panel (`#los-panel`) with terrain profile visualization
- Chart.js CDN for elevation chart
- `interpolatePoints()` - generate points between two coordinates
- `fetchElevationData()` - fetch terrain elevation from Open-Elevation API
- `fresnelRadius()` - calculate Fresnel zone radius for 868MHz
- `checkLOS()` - analyze line of sight with Fresnel zone clearance
- `showLOSPanel()` - display LOS analysis for clicked node
- `closeLOSPanel()` - close LOS panel and cleanup chart
- "Show LOS on click" checkbox in Mesh Stats panel
- CSS styles for LOS panel with animation

**Changed:**
- Marker click behavior: shows LOS panel for hops=0 nodes when checkbox enabled
- Antenna height calculation: uses `max(Alt, terrain) + 10m` offset
- Version bumped to v1.11 (`MAPPER_VERSION`, `styles.css?v=1.11`)

## Previous Changes (v1.10)

**Added:**
- `isOwnTracker()` function to identify own tracker node
- Blue marker style `.marker.tracker-home` for own tracker
- Distance calculation in popup for hops=0 nodes (using `calculateDistance()`)
- Role field in node popup
- "Hide unknown hops" checkbox filter
- Special popup for own tracker showing: name, YOUR TRACKER, Role, Pos, Alt

**Changed:**
- Popup font changed to Inter for better readability
- Own tracker excluded from "Hide unknown hops" filter
- Version bumped to v1.10 (`MAPPER_VERSION`, `styles.css?v=1.10`)

## Previous Changes (v1.9)

**Added:**
- Safari detection: `isSafari` flag using userAgent regex (`/^((?!chrome|android).)*safari/i`)
- `WS_CONNECT_DELAY` constant: 500ms for Safari, 100ms for others
- `WS_RETRY_DELAY` constant: 2000ms for Safari, 1000ms for others

## Previous Changes (v1.8)

**Added:**
- Direct connection lines: checkbox "Show direct lines"; draws Leaflet polylines from tracker to all hops=0 nodes, color-coded by SNR (green ≥5, yellow ≥-5, red <-5), opacity 0.6, weight 2px
- Heat map: checkbox "Show heat map"; uses leaflet.heat CDN (radius 25, blur 15, maxZoom 17)
- `snrToColor()`, `updateDirectLines()`, `updateHeatMap()` functions
- `.filter-label` CSS class for consistent checkbox styling

## Coding Conventions

**Python:**
- Functions use snake_case
- Classes use PascalCase (`ListenBasedMapper`)
- Global variables for WebSocket clients (`connected_clients`)
- Async functions for WebSocket handlers
- Print statements for logging (stdout/journal)
- Dict-based data storage (not classes/dataclasses)
- Comment headers show version and author

**JavaScript:**
- Vanilla JS (no frameworks)
- camelCase for variables and functions
- Global state variables at top of script
- Inline in HTML (no separate .js file)
- Console logging for debugging
- Retry logic with exponential backoff

**CSS:**
- Separate file (styles.css)
- Compact formatting (properties on same line for simple rules)
- Color codes: green=#22c55e, yellow=#eab308, red=#ef4444, blue=#3b82f6
- Position fixed for overlay panels
- Collapsible panels via .open class toggle

## Development Notes

**No build process**: Project uses vanilla HTML/CSS/JS with CDN-loaded libraries. Changes to frontend files require only copying to `/var/www/html/meshtastic/` and browser refresh.

**Testing**: No automated tests. Manual testing via:
- `python3 backend/meshtastic_mapper.py` (direct run)
- `sudo systemctl restart meshtastic-mapper` (service test)
- `sudo journalctl -u meshtastic-mapper -f` (view logs)
- Browser console for frontend debugging

**Parser robustness**: Parsers use regex and `ast.literal_eval()` to extract data from `meshtastic --listen` output. Format depends on meshtastic CLI version - parsers may need updates if CLI output format changes.

**WebSocket architecture**: Single-threaded async WebSocket server runs in daemon thread. Uses `websockets.broadcast()` for efficient multi-client messaging. No message queuing. Messages are persisted to `nodes.json` and survive restarts.

**State management**: Backend maintains authoritative state in dicts. Frontend state is derived from WebSocket updates or periodic JSON fetches. No database.

**Performance**: Optimized for Raspberry Pi Model B+ (512MB RAM). Python process uses ~40MB RAM, 15-20% CPU. WebSocket overhead is minimal (~1KB per update).

## Common Tasks

**Modify node TTL:**
- Edit `max_age=172800` in the `ListenBasedMapper(...)` instantiation in `__main__`
- Restart service: `sudo systemctl restart meshtastic-mapper`

**Change WebSocket port:**
- Edit `websockets.serve(websocket_handler, "0.0.0.0", 8765)` in `start_websocket_server()`
- Edit `const wsUrl = \`ws://\${window.location.hostname}:8765\`` in `frontend/index.html` (line ~194)
- Restart service and refresh browser

**Add new parser:**
- Create method `parse_xxx_update(self, line)` in `ListenBasedMapper` class
- Call it in the main readline loop in `run()`
- Add WebSocket broadcast call with `asyncio.run(self.broadcast_xxx(data))`
- Create corresponding handler in frontend WebSocket `onmessage`

**Add new WebSocket message type:**
- Add handler in `websocket_handler()` in the `async for message in websocket:` block
- Add corresponding `elif data.type === '...'` branch in frontend `ws.onmessage`

**Debugging parsers:**
- Run backend directly: `python3 backend/meshtastic_mapper.py`
- Watch for `[RECV]` lines showing raw meshtastic output
- Add print statements in parser methods to see extracted data
- Check logs: `sudo journalctl -u meshtastic-mapper -f`

## Known Issues & Limitations

- No authentication on WebSocket server (assumes trusted LAN)
- MQTT nodes appear in data but excluded from max range calculation
- Relies on meshtastic CLI output format (may break with CLI updates)
- Safari requires `map.whenReady()` before initial data load
- Heltec V3 may require `--no-nodes` flag to avoid timeouts
- No rate limiting on WebSocket connections
- Traceroute on serial pauses the listener for ~60s (USB port is not shared)
- Open-Elevation API is a free public service; may be slow or unavailable
