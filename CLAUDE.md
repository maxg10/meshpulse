# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Meshtastic Network Mapper is a real-time web visualization tool for Meshtastic mesh network nodes. It connects to a Meshtastic device via USB serial, parses node/position/telemetry packets from `meshtastic --listen`, and displays nodes on an interactive Leaflet.js map. Optimized for low-power devices (Raspberry Pi Model B+, 512MB RAM).

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

Single Python file (~618 lines) with the `ListenBasedMapper` class:

- **Subprocess model**: Spawns `meshtastic --listen` as a subprocess, parses stdout line-by-line in real-time. Auto-restarts on process termination.
- **Three parsers**: `parse_node_info()`, `parse_position_update()`, `parse_telemetry_update()` — each handles a different packet type from the meshtastic CLI output.
- **Dual data stores**: `self.nodes` (nodes with GPS positions) and `self.nodes_no_pos` (nodes without GPS). Both are dicts keyed by node ID (e.g., `!7b6c8272`).
- **JSON output**: Writes `nodes.json` every 60 seconds to the web server directory. Stale nodes cleaned every hour based on `max_age` (default 24h TTL).
- **WebSocket server**: Async server on port 8765 running in a separate thread via `threading.Thread` + `asyncio`. Broadcasts `node_update` messages to all connected clients on each packet.
- **Distance**: Haversine formula in `calculate_distance()` for km distances; `get_max_distance()` finds the farthest directly-reachable node (hops=0).

### Frontend (`frontend/index.html` + `frontend/styles.css`)

Vanilla JS single-page app with Leaflet.js v1.9.4:

- **Data fetching**: Primary WebSocket connection with fallback to JSON polling every 15 seconds. Exponential backoff retry (max 5 attempts).
- **Node markers**: Color by age (green <1h, yellow 1-6h, red >6h). Shape by role (square=router, circle=client). Dashed border for relayed nodes (hops>0). Offset logic prevents marker overlap.
- **UI panels**: Mesh Stats (counts, avg SNR, max range, tracker info), No-GPS panel (nodes without coordinates), Legend. All collapsible.
- **Status indicator**: Green=WebSocket connected, Yellow=polling fallback, Red=disconnected.

### Data Flow

```
Meshtastic device (USB) → meshtastic --listen (subprocess) → Python parser
  → nodes dict → nodes.json (every 60s) → frontend polling
  → WebSocket broadcast (real-time) → frontend WebSocket
```

## Key Configuration (in `meshtastic_mapper.py`)

- `self.port`: Serial port (`/dev/ttyUSB0`, falls back to `/dev/ttyACM0`)
- `self.json_path`: Output location (`/var/www/html/meshtastic/nodes.json`)
- `self.max_age`: Node TTL in seconds (default `86400` = 24h)

## Dependencies

- Python 3.7+, `meshtastic` CLI, `websockets` library
- lighttpd or apache2 for serving frontend
- User must be in `dialout` group for USB serial access
