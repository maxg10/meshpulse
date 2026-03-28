# Deployment

## Run manually
```bash
python3 backend/meshtastic_mapper.py
```

## Install as systemd service
```bash
./install.sh
```

Copies frontend to `/var/www/html/meshtastic/`, generates systemd service from template.
- Web: `http://<host>/meshtastic/`
- WebSocket: `ws://<host>:8765`

## Service management
```bash
sudo systemctl start meshtastic-mapper
sudo systemctl status meshtastic-mapper
sudo journalctl -u meshtastic-mapper -f
```

## Docker (TCP-only, no USB)
```bash
# Option A — tracker IP upfront:
docker run -e TRACKER_HOST=192.168.1.103 -p 80:80 -p 8765:8765 \
  -v mapper-data:/var/www/html/meshtastic maxg10/meshtastic-mapper

# Option B — configure via web UI:
cp .env.example .env
docker compose up -d
```

Data persists in `mapper-data` Docker volume (`nodes.json`, `stats.db`, `config.json`).

## Dependencies

**Python:** Python 3.7+, `meshtastic` CLI (`pip3 install meshtastic`), `websockets` (`pip3 install websockets`)
**System:** lighttpd or apache2, systemd, user in `dialout` group
**Frontend:** Leaflet.js v1.9.4, leaflet.heat v0.2.0, Chart.js — all from CDN
