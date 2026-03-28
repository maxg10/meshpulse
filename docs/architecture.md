# Architecture

## Backend (`backend/meshtastic_mapper.py`)

Single Python file (~1225 lines), class `ListenBasedMapper`.

- **Subprocess model**: Spawns `meshtastic --listen`, parses stdout line-by-line. Auto-restarts on termination.
- **Four parsers**: `parse_node_info()`, `parse_position_update()`, `parse_telemetry_update()`, `parse_text_message()`
- **Dual data stores**: `self.nodes` (with GPS) and `self.nodes_no_position` (without GPS) — dicts keyed by node ID e.g. `!7b6c8272`
- **Messages storage**: `self.messages` list, up to 50 entries, newest first, persisted to `nodes.json`
- **Source tracking**: `source` field — `'memory'` on load, `'live'` on real packet
- **JSON output**: Writes `nodes.json` every 60s. Stale nodes cleaned every hour based on `max_age` (default 48h)
- **WebSocket server**: Async on port 8765, separate thread via `threading.Thread` + `asyncio`

### WebSocket message types
- `node_update` — real-time node data
- `node_deleted` — TTL cleanup
- `new_message` — text messages (broadcast + DM)
- `stats_update` — max distance / farthest node
- `connection_status` — tracker connection state + info
- `traceroute_status` — progress (starting/reconnecting)
- `traceroute_result` — hop data + raw output

### Traceroute
- **TCP mode**: Runs alongside listener, no interruption. Timeout: 60s.
- **Serial mode**: Terminates listener, waits 2s, runs traceroute, then sets `traceroute_restart=True` + `restart_event.set()`
- **Parser**: `parse_traceroute_output()` splits on `-->`, extracts hop names + SNR via regex
- **Frontend**: SNR-colored polylines (solid=forward, dashed=return), 90s safety timeout

### Radio Stats
`parse_telemetry_update()` extracts `localStats` only when `node_id == self.local_node_id`:
fields: `channelUtilization`, `airUtilTx`, `numPacketsTx`, `numPacketsRx`, `numPacketsRxBad`, `numRxDupe`, `numTxRelay`, `numOnlineNodes`, `numTotalNodes`

### RSSI
- Position packets: `rxRssi` → `self.nodes[node_id]['rssi']`
- Telemetry packets: `rxRssi` → updates both `self.nodes` and `self.nodes_no_position`

### LOS Analysis (`checkLOS()`)
- Earth curvature: `curvatureCorrection = (d1 * d2) / (2 * R_eff)` where `R_eff = 8500000m` (k=4/3)
- Fresnel zone: 60% clearance at 868MHz — `sqrt(wavelength * d1 * d2 / (d1 + d2))`
- Antenna height: `max(node.alt, terrain_elev) + 10m`
- Elevation API: `https://api.open-elevation.com/api/v1/lookup`, 50 sample points

### StatsDB
- **Path**: `/var/www/html/meshtastic/stats.db`
- **Tables**: `packets`, `node_activity`, `anomalies`
- **Retention**: 3 days, cleanup on every `save_nodes()`
- **Thread safety**: `threading.Lock()` on all DB connections

## Frontend (`frontend/index.html` + `styles.css`)

Vanilla JS, Leaflet.js v1.9.4. No build tools.

- **Data fetching**: WebSocket primary → JSON polling fallback (15s), exponential backoff (max 5 attempts)
- **Node markers**: Color by age (green <1h, yellow 1-6h, red >6h), shape by role (square=router, circle=client), dashed=relayed
- **UI panels**: Mesh Stats, No-GPS, Radio, Messages, Legend, LOS, Traceroute — all collapsible

## Data Flow
```
Device (USB/TCP) → meshtastic --listen → Python parser
  → nodes dict + messages list
  → nodes.json (60s) → frontend polling
  → WebSocket broadcast → frontend
```
