# Meshtastic Network Mapper

Real-time web-based visualization of Meshtastic mesh network nodes. Optimized for low-power devices like Raspberry Pi Model B+.

![Meshtastic Network Map](docs/screenshot.png)

## Features

- ? **Real-time node tracking** - Live position updates via meshtastic CLI
- ??? **Interactive map** - Leaflet.js-based web interface  
- ?? **Auto-restart** - Resilient to connection timeouts
- ?? **JSON API** - Easy integration with other tools
- ?? **Slow hardware support** - Works on Raspberry Pi Model B+ (512MB RAM)

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

# 2. Clone repository
git clone https://github.com/maxg10/meshtastic-network-mapper.git
cd meshtastic-network-mapper

# 3. Setup directories
sudo mkdir -p /var/www/html/meshtastic
sudo cp frontend/index.html /var/www/html/meshtastic/
sudo chown -R $USER:$USER /var/www/html/meshtastic

# 4. Install backend
cp backend/meshtastic_mapper.py ~/meshtastic_mapper.py
chmod +x ~/meshtastic_mapper.py

# 5. Test manually
python3 ~/meshtastic_mapper.py
```

### Systemd Service (Optional)
```bash
# Install service
sudo cp systemd/meshtastic-mapper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-mapper
sudo systemctl start meshtastic-mapper

# Check status
sudo systemctl status meshtastic-mapper
sudo journalctl -u meshtastic-mapper -f
```

## Configuration

Edit paths in `backend/meshtastic_mapper.py` if needed:
```python
self.port = '/dev/ttyUSB0'  # Change if tracker on different port
self.json_path = '/var/www/html/meshtastic/nodes.json'  # Output path
```

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
  "nodes": [
    {
      "id": "!7b6c8272",
      "name": "maxmesh router PL",
      "lat": 52.353434,
      "lon": 16.865690,
      "alt": 75,
      "snr": 10.8,
      "ts": 1736625600
    }
  ]
}
```

## Troubleshooting

### Tracker not detected
```bash
# Check USB devices
ls -la /dev/ttyUSB* /dev/ttyACM*

# Check meshtastic connection
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
