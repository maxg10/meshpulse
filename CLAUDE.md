# CLAUDE.md

**Author:** Mariusz "Max" Gieparda | mgieparda@yahoo.com | github.com/maxg10

## Project Overview

MeshPulse — real-time web visualization for Meshtastic mesh network nodes.
Connects via USB serial or TCP, displays nodes on Leaflet.js map with WebSocket updates.
Optimized for Raspberry Pi Model B+ (512MB RAM).

**Current Version:** v2.6.1

## Key Paths & Ports

| What | Where |
|------|-------|
| Backend | `backend/meshpulse.py` (~1225 lines, class `ListenBasedMapper`) |
| Frontend | `frontend/index.html`, `styles.css`, `config.html`, `stats.html`, `messages.html` |
| Web root | `/var/www/html/meshtastic/` |
| WebSocket | port `8765` |
| Data files | `nodes.json` (60s), `stats.db`, `config.json` |
| Node TTL | `max_age=172800` (48h) in `__main__` |

## Quick Start
```bash
python3 backend/meshpulse.py   # run directly
./install.sh                           # install as systemd service
sudo systemctl restart meshpulse
sudo journalctl -u meshpulse -f
```

## File Structure
```
backend/meshpulse.py
frontend/{index,config,stats,messages}.html + styles.css
systemd/meshpulse.service.template
install.sh  README.md  Dockerfile  docker/
docs/
  architecture.md   ← backend/frontend/websocket/LOS/StatsDB details
  deployment.md     ← Docker, systemd, dependencies
  development.md    ← conventions, common tasks, known issues
  changelog.md      ← full history v1.8→v2.1.0
```

## Architecture (summary)

- Backend spawns `meshtastic --listen` subprocess, parses stdout, broadcasts via WebSocket
- Four parsers: `parse_node_info/position_update/telemetry_update/text_message()`
- Dual stores: `self.nodes` (GPS) + `self.nodes_no_position`
- Frontend: Vanilla JS + Leaflet.js, WebSocket primary / JSON polling fallback
- TCP mode: Python `TCPMeshtasticInterface`; Serial mode: `SerialMeshtasticInterface`

→ Full details in `docs/architecture.md`

## No build process

Pure HTML/CSS/JS + CDN libraries. Deploy = copy files to `/var/www/html/meshtastic/` + browser refresh.
No test suite. No linter.
