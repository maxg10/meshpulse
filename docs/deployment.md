# Deployment

## Run manually
```bash
python3 backend/meshpulse.py
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
sudo systemctl start meshpulse
sudo systemctl status meshpulse
sudo journalctl -u meshpulse -f
```

## Updating
```bash
cd ~/meshpulse
./update.sh   # git pull + install + restart in one command
```

## Docker (TCP-only, no USB)
```bash
# Option A — tracker IP upfront:
docker run -e TRACKER_HOST=192.168.1.103 -p 80:80 -p 8765:8765 \
  -v mapper-data:/var/www/html/meshtastic maxg10/meshpulse

# Option B — configure via web UI:
cp .env.example .env
docker compose up -d
```

Data persists in `mapper-data` Docker volume (`nodes.json`, `stats.db`, `config.json`).

## Dependencies

**Python:** Python 3.7+, `meshtastic` CLI (`pip3 install meshtastic`), `websockets` (`pip3 install websockets`)
**System:** lighttpd or apache2, systemd, user in `dialout` group
**Frontend:** Leaflet.js v1.9.4, leaflet.heat v0.2.0, Chart.js — all from CDN
