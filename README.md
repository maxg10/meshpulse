# Meshtastic Network Mapper

Real-time web-based visualization of Meshtastic mesh network nodes. Optimized for low-power devices like Raspberry Pi Model B+.

![Meshtastic Network Map](docs/screenshot.png)

## Features

## Features

- 📡 **Real-time node tracking** - Live position updates via meshtastic CLI
- 🗺️ **Interactive map** - Leaflet.js-based web interface  
- ⏰ **TTL (Time-To-Live)** - Automatic cleanup of stale nodes (24h default)
- 🔄 **Auto-restart** - Resilient to connection timeouts
- 📋 **JSON API** - Easy integration with other tools
- 📂 **Multi-tracker support** - Merge data from multiple trackers
- 🐢 **Slow hardware support** - Works on Raspberry Pi Model B+ (512MB RAM)
- 📏 **Max range display** - Shows distance to farthest directly reachable node (hops=0)
- 🎯 **Accurate node status** - Shows real last-heard time from tracker memory

## Node Colors (Real Network State)

The map shows **true network status** based on when your tracker last heard each node:

| Color | Age | Meaning |
|-------|-----|---------|
| 🟢 Green | < 1 hour | Recently heard - node is active |
| 🟡 Yellow | 1-6 hours | Not heard recently - may be inactive or out of range |
| 🔴 Red | > 6 hours | Stale - tracker hasn't heard this node for a long time |

**Note:** This reflects your tracker's perspective. A "red" node may still be active in the mesh but simply not heard by your specific tracker due to distance or obstacles.

### Shape Indicators
- **Square** = Router node
- **Circle** = Client node
- **Dashed border** = Relayed (hops > 0)

## Quick Start

### Requirements

- Raspberry Pi (Model B+ or newer) or similar Linux system
- Meshtastic tracker connected via USB
- Python 3.7+
- lighttpd or Apache2 web server

### Installation
```bash
# 1. Install dependencies
sudo apt update
sudo apt install python3 python3-pip lighttpd -y
pip3 install meshtastic --break-system-packages

# 2. Start and enable web server
sudo systemctl enable lighttpd
sudo systemctl start lighttpd
sudo systemctl status lighttpd

# 3. Clone repository
git clone https://github.com/maxg10/meshtastic-network-mapper.git
cd meshtastic-network-mapper

# 4. Setup web directory
sudo mkdir -p /var/www/html/meshtastic
sudo cp frontend/index.html /var/www/html/meshtastic/
sudo chown -R $USER:$USER /var/www/html/meshtastic

# 5. Install backend
cp backend/meshtastic_mapper.py ~/meshtastic_mapper.py
chmod +x ~/meshtastic_mapper.py

# 6. Test manually (important!)
python3 ~/meshtastic_mapper.py
# Press Ctrl+C after 2-3 minutes once you see nodes appearing

# 7. Verify JSON was created
cat /var/www/html/meshtastic/nodes.json

# 8. Open in browser to test
# http://YOUR_PI_IP/meshtastic/

# 9. Install systemd service (run on boot)
sudo cp systemd/meshtastic-mapper.service /etc/systemd/system/

# IMPORTANT: Edit service file to match your username
sudo vi /etc/systemd/system/meshtastic-mapper.service
# Change User=maxg and /home/maxg to your username

# 10. Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-mapper
sudo systemctl start meshtastic-mapper
sudo systemctl status meshtastic-mapper
```


## Updating

If you already have meshtastic-network-mapper installed and want to update to the latest version:
```bash
# 1. Navigate to repository
cd ~/meshtastic-network-mapper

# 2. Pull latest changes from GitHub
git pull origin main

# 3. Check what changed
git log --oneline -5

# 4. Update backend script
cp backend/meshtastic_mapper.py ~/meshtastic_mapper.py

# 5. Update frontend (if changed)
sudo cp frontend/index.html /var/www/html/meshtastic/index.html

# 6. Update systemd service
sudo cp systemd/meshtastic-mapper.service /etc/systemd/system/

# IMPORTANT: Verify user in service file
sudo vi /etc/systemd/system/meshtastic-mapper.service
# Check that User= and WorkingDirectory= match your username

# 7. Reload systemd and restart service
sudo systemctl daemon-reload
sudo systemctl restart meshtastic-mapper

# 8. Verify it's running
sudo systemctl status meshtastic-mapper

# 9. Check logs for new features
sudo journalctl -u meshtastic-mapper -f
```

### What's New in Latest Version


**v1.4 - Accurate Node Status**
- 🎯 Uses real `lastHeard` timestamp from tracker's memory
- ♡ Telemetry heartbeat keeps local nodes fresh
- 🔴 Node colors now reflect true network state (when tracker actually heard the node)
- 🐛 Fixed: nodes no longer appear "stale" due to infrequent position updates

**v1.3 - Max Range Feature**
- 📏 Distance calculation to farthest directly reachable node (hops=0)
- 🔍 Click to locate farthest node on map
- 🆔 Auto-detection of local node ID via `meshtastic --no-nodes --info`
- 📊 Max distance displayed in stats panel

**v1.2 - TTL & Multi-Tracker Support**
- ⏰ Automatic cleanup of nodes older than 24 hours (configurable)
- 📂 Load existing nodes from previous runs (merge data from multiple trackers)
- ✚/↻ Visual indicators for new vs updated nodes
- 🔢 Shows node count on startup
- 🧹 Hourly cleanup of stale nodes

Check the logs after update - you should see:
```
[LOAD] Found X existing nodes from previous run
[LOAD] Loaded Y nodes after cleanup
? !7b6c8272 maxmesh router PL @ 52.3534,16.8657  (existing)
? !NEW12345 New Node @ 52.4000,16.9000          (new!)
```

### Backup Before Update (Optional)
```bash
# Backup current nodes.json
cp /var/www/html/meshtastic/nodes.json ~/nodes_backup_$(date +%Y%m%d).json

# Backup current script
cp ~/meshtastic_mapper.py ~/meshtastic_mapper_backup.py
```

### Rollback if Needed
```bash
# Restore old script
cp ~/meshtastic_mapper_backup.py ~/meshtastic_mapper.py
sudo systemctl restart meshtastic-mapper

# Or go back to specific git version
cd ~/meshtastic-network-mapper
git log --oneline  # Find commit hash
git checkout <commit-hash> backend/meshtastic_mapper.py
cp backend/meshtastic_mapper.py ~/meshtastic_mapper.py
sudo systemctl restart meshtastic-mapper
```


### Systemd Service (Run on boot)

The service file uses systemd variables (`%u` for username, `%h` for home directory) to work with any user account.
```bash
# Install service
sudo cp systemd/meshtastic-mapper.service /etc/systemd/system/

# If you customized the installation path, edit the service file:
# sudo vi /etc/systemd/system/meshtastic-mapper.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-mapper
sudo systemctl start meshtastic-mapper

# Check status
sudo systemctl status meshtastic-mapper

# View live logs
sudo journalctl -u meshtastic-mapper -f
```

## Configuration

Edit paths in `backend/meshtastic_mapper.py` if needed:
```python
self.port = '/dev/ttyUSB0'  # Change if tracker on different port
self.json_path = '/var/www/html/meshtastic/nodes.json'  # Output path
self.max_age = 86400  # Node TTL in seconds (24h default)
```

### Change TTL (Time-To-Live)

Nodes older than `max_age` seconds are automatically removed:
```python
# In backend/meshtastic_mapper.py, line ~237:
mapper = ListenBasedMapper(port, max_age=86400)  # 24 hours
# Change to:
mapper = ListenBasedMapper(port, max_age=43200)  # 12 hours
# Or:
mapper = ListenBasedMapper(port, max_age=172800)  # 48 hours
```

### Systemd Service User

The service file has `User=maxg` hardcoded. Change it to your username:
```bash
sudo vi /etc/systemd/system/meshtastic-mapper.service
# Change: User=maxg
# To:     User=pi  (or your username)
```
### Max Range Requirements

For the max range feature to work, your local tracker (connected to Raspberry Pi) must have a position set. This can be either:
- **GPS** - tracker with built-in GPS that reports position automatically
- **Fixed position** - manually set coordinates:
```bash
~/.local/bin/meshtastic --port /dev/ttyUSB0 --setlat 52.XXXXX --setlon 16.XXXXX
```

Without position data, max range will show "-" in the stats panel.


## Architecture

### Backend (`backend/meshtastic_mapper.py`)

- Uses `meshtastic --listen` mode to capture node information
- Parses debug output to extract position data
- Saves to JSON every 60 seconds
- Auto-restarts on timeout/errors

### Frontend (`frontend/index.html`)

- Leaflet.js for interactive map
- Polls `nodes.json` every 15 seconds
- Shows node name, ID, SNR, altitude
- Clean, responsive design

### Data Format
```json
{
  "ts": 1736625600,
  "updated": "2025-01-11T18:40:00",
  "cnt": 7,
  "max_distance_km": 7.0,
  "farthest_node": "!e36738ab",
  "nodes": [
    {
      "id": "!7b6c8272",
      "name": "maxmesh router PL",
      "lat": 52.353434,
      "lon": 16.865690,
      "alt": 75,
      "snr": 10.8,
      "role": "ROUTER",
      "hops": 0,
      "ts": 1736625600
    }
  ]
}
```

## Troubleshooting

### Web server not running
```bash
# Check lighttpd status
sudo systemctl status lighttpd

# Start if stopped
sudo systemctl start lighttpd

# Check if port 80 is listening
sudo netstat -tlnp | grep :80
```

### Service fails with "No such file or directory"
```bash
# Verify the script is in the right location
ls -la ~/meshtastic_mapper.py

# Check service file paths
sudo systemctl cat meshtastic-mapper.service

# Service runs as the user who installed it
# Make sure the script path matches: ~/meshtastic_mapper.py
```

### Tracker not detected
```bash
# Check USB devices
ls -la /dev/ttyUSB* /dev/ttyACM*

# Check if user has access (add to dialout group)
sudo usermod -aG dialout $USER
# Log out and back in for group changes to take effect

# Test meshtastic connection
meshtastic --port /dev/ttyUSB0 --info
```

### Service fails to start
```bash
# Check logs
sudo journalctl -u meshtastic-mapper -n 50

# Test manually
python3 ~/meshtastic_mapper.py
```

### Map shows 0 nodes
```bash
# Check JSON file
cat /var/www/html/meshtastic/nodes.json

# Verify web server
curl http://localhost/meshtastic/nodes.json
```
### Heltec V3 timeout issues
If you're using Heltec V3 and getting "Timed out waiting for connection completion" errors, add `--no-nodes` flag to the command in `backend/meshtastic_mapper.py`:
```python
cmd = [self.meshtastic_cmd, '--port', self.port, '--listen', '--no-nodes']
```

## Performance Notes

**Raspberry Pi Model B+ (512MB RAM):**
- Initial node discovery: ~2-3 minutes
- CPU usage: ~15-20% average
- RAM usage: ~40MB for Python process
- Works reliably with 10+ nodes

**Faster devices:** Will have near-instant node discovery.

## Known Limitations

- **Timeout issues:** `meshtastic --nodes` times out on slow Pi - we use `--listen` mode instead
- **No real-time packets:** Only updates when nodes broadcast position (every 15-30 min typically)
- **MQTT not supported:** Direct USB connection only

## Contributing

Pull requests welcome! Areas for improvement:

- [ ] MQTT support for faster updates
- [ ] WebSocket for real-time updates
- [ ] Node history/trails on map
- [ ] Network topology visualization
- [ ] Mobile-friendly UI improvements

## License

MIT License - See LICENSE file

## Credits

Built for the Polish Meshtastic community around Pozna? ????

- Meshtastic: https://meshtastic.org
- Leaflet.js: https://leafletjs.com
- loranet.pl MQTT server

## Screenshots

![Network Map](docs/screenshot.png)
*7 active nodes in Poznan metro area*

---

**Questions?** Open an issue on GitHub!
