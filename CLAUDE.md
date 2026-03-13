# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Meshtastic Network Mapper is a real-time web visualization tool for Meshtastic mesh network nodes. It connects to a Meshtastic device via USB serial, parses node/position/telemetry packets from `meshtastic --listen`, and displays nodes on an interactive Leaflet.js map with WebSocket support for real-time updates. Optimized for low-power devices (Raspberry Pi Model B+, 512MB RAM).

**Current Version:** v1.8 (direct connection lines + heat map)

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

Single Python file (~734 lines) with the `ListenBasedMapper` class:

- **Subprocess model**: Spawns `meshtastic --listen` as a subprocess, parses stdout line-by-line in real-time. Auto-restarts on process termination.
- **Four parsers**: `parse_node_info()`, `parse_position_update()`, `parse_telemetry_update()`, `parse_text_message()` — each handles a different packet type from the meshtastic CLI output.
- **Dual data stores**: `self.nodes` (nodes with GPS positions) and `self.nodes_no_position` (nodes without GPS). Both are dicts keyed by node ID (e.g., `!7b6c8272`).
- **Messages storage**: `self.messages` list stores up to 50 recent text messages (broadcasts and DMs), newest first. Persisted to `nodes.json` and reloaded on startup.
- **JSON output**: Writes `nodes.json` every 60 seconds to the web server directory. Includes nodes, no-GPS nodes, messages, and tracker info. Stale nodes cleaned every hour based on `max_age` (default 7 days / 604800 seconds).
- **WebSocket server**: Async server on port 8765 running in a separate thread via `threading.Thread` + `asyncio`. Broadcasts three message types to all connected clients:
  - `node_update` - real-time node data updates
  - `node_deleted` - when old nodes are removed
  - `new_message` - text messages (broadcasts and DMs)
- **Serial port detection**: Auto-detects USB serial port from list of common ports (`/dev/ttyUSB[0-2]`, `/dev/ttyACM[0-2]`)
- **Tracker info**: Extracts local node ID, hardware model, firmware version, and uptime via `meshtastic --info`
- **Distance**: Haversine formula in `calculate_distance()` for km distances; `get_max_distance()` finds the farthest directly-reachable node (hops=0).

### Frontend (`frontend/index.html` + `frontend/styles.css`)

Vanilla JS single-page app with Leaflet.js v1.9.4:

- **Data fetching**: Primary WebSocket connection (`ws://host:8765`) with automatic fallback to JSON polling every 15 seconds. Exponential backoff retry (max 5 attempts). Auto-reconnects on WebSocket disconnect.
- **Node markers**: Color by age (green <1h, yellow 1-6h, red >6h). Shape by role (square=router, circle=client). Dashed border for relayed nodes (hops>0). Offset logic prevents marker overlap.
- **UI panels**: All collapsible, positioned on map:
  - **Mesh Stats** (top-right): Node counts, avg SNR, max range, tracker info (model, firmware, ID, port, uptime), filter checkbox for direct-only nodes, connection status indicator
  - **No-GPS panel** (top-right below stats): Lists nodes without GPS coordinates, shows SNR/hops/age
  - **Messages panel** (bottom-left): Shows up to 50 recent text messages (broadcasts and DMs), DMs highlighted in blue
  - **Legend** (bottom-left): Visual reference for marker colors, shapes, and connection types
- **Status indicator**: Green dot = WebSocket connected (real-time), Yellow = polling fallback, Red = disconnected
- **Real-time updates**: WebSocket messages trigger immediate UI updates without full page refresh
- **Message display**: Shows sender name, timestamp (HH:MM format), message text, DM indicator
- **Cache busting**: No-cache meta tags in `<head>`. CSS loaded with `?v=1.7` suffix. `MAPPER_VERSION` constant in JS — increment both when assets change. `nodes.json` already uses `?` + `Date.now()`

### Data Flow

```
Meshtastic device (USB) → meshtastic --listen (subprocess) → Python parser
  → nodes dict + messages list
  → nodes.json (every 60s) → frontend polling (fallback)
  → WebSocket broadcast (real-time) → frontend WebSocket
    ├── node_update events (position/telemetry/nodeinfo)
    ├── node_deleted events (TTL cleanup)
    └── new_message events (text messages)
```

## Key Configuration (in `meshtastic_mapper.py`)

- `self.port`: Serial port (auto-detected from `/dev/ttyUSB[0-2]` or `/dev/ttyACM[0-2]`)
- `self.json_path`: Output location (`/var/www/html/meshtastic/nodes.json`)
- `self.max_age`: Node TTL in seconds (default `604800` = 7 days, configurable in line 725)
- `self.meshtastic_cmd`: Path to meshtastic CLI (`~/.local/bin/meshtastic`)
- WebSocket port: `8765` (hardcoded in websocket server, line 682)
- Save interval: `60` seconds (line 572)
- Cleanup interval: `3600` seconds / 1 hour (line 573)
- Max messages: `50` messages stored (line 36, 463)

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
- No build tools required - pure HTML/CSS/JS

## File Structure

```
meshtastic-network-mapper/
├── backend/
│   └── meshtastic_mapper.py      # Main Python backend (734 lines)
├── frontend/
│   ├── index.html                # Main web interface with inline JS
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

## Recent Changes (v1.8)

**Added:**
- Direct connection lines: checkbox "Show direct lines" in Mesh Stats panel; draws Leaflet polylines from tracker to all hops=0 nodes, color-coded by SNR (green ≥5, yellow ≥-5, red <-5), opacity 0.6, weight 2px
- Heat map: checkbox "Show heat map" in Mesh Stats panel; uses leaflet.heat CDN to render node density layer (radius 25, blur 15, maxZoom 17)
- `snrToColor()`, `updateDirectLines()`, `updateHeatMap()` functions in frontend JS
- Global state: `directLines[]` array for polyline lifecycle, `heatLayer` for heat map lifecycle
- `.filter-label` CSS class for consistent checkbox styling

**Changed:**
- Version bumped to v1.8 (`MAPPER_VERSION`, `styles.css?v=1.8`, stats panel display)
- `filter-direct` label uses `.filter-label` CSS class (was inline style)

## Previous Changes (v1.7)

**Added:**
- Message persistence: messages saved to `nodes.json` and reloaded on startup/page refresh
- Cache busting: no-cache meta tags, `styles.css?v=1.7`, `MAPPER_VERSION` JS constant

## Previous Changes (v1.6)

**Added:**
- WebSocket server for real-time updates (no page refresh needed)
- Text message parsing and display (broadcasts and DMs)
- Messages panel in UI with up to 50 recent messages
- Serial port auto-detection
- Node deletion broadcasts when TTL expires
- Uptime tracking for local tracker node
- Separate styles.css file (refactored from inline)

**Changed:**
- Default TTL increased from 24h to 7 days (604800 seconds)
- Connection status indicator now shows WebSocket/polling/disconnected states
- Frontend prioritizes WebSocket, falls back to polling on disconnect
- Improved Safari compatibility (map.whenReady() fix)

**Fixed:**
- Node timestamp handling (uses `ts` field consistently)
- Old node cleanup now broadcasts deletions to WebSocket clients
- WebSocket reconnection logic with exponential backoff

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
- Color codes: green=#22c55e, yellow=#eab308, red=#ef4444
- Position absolute for overlay panels
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
- Edit line 725 in `backend/meshtastic_mapper.py`: `mapper = ListenBasedMapper(port, max_age=604800)`
- Restart service: `sudo systemctl restart meshtastic-mapper`

**Change WebSocket port:**
- Edit line 682 in `backend/meshtastic_mapper.py`: `async with websockets.serve(websocket_handler, "0.0.0.0", 8765)`
- Edit line 112 in `frontend/index.html`: `const wsUrl = \`ws://\${window.location.hostname}:8765\``
- Restart service and refresh browser

**Add new parser:**
- Create method `parse_xxx_update(self, line)` in `ListenBasedMapper` class
- Call it in main loop (line 605)
- Add WebSocket broadcast call with `asyncio.run(self.broadcast_xxx(data))`
- Create corresponding handler in frontend WebSocket onmessage (line 136)

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
