# Development

## Coding Conventions

**Python:** snake_case functions, PascalCase classes, global `connected_clients`, async WS handlers, print for logging, dict-based storage.
**JavaScript:** Vanilla JS, camelCase, global state at top, inline in HTML, exponential backoff retry.
**CSS:** Separate `styles.css`. Colors: green=#22c55e, yellow=#eab308, red=#ef4444, blue=#3b82f6. Fixed-position panels, `.open` class toggle.

## Common Tasks

**Change node TTL:** Edit `max_age=172800` in `__main__`, restart service.

**Change WebSocket port:** Edit `websockets.serve(..., 8765)` in `start_websocket_server()` + `const wsUrl` in `frontend/index.html` line ~194.

**Add new parser:** Create `parse_xxx_update(self, line)` in `ListenBasedMapper`, call in readline loop, broadcast via `asyncio.run(self.broadcast_xxx(data))`, add frontend `onmessage` handler.

**Add new WebSocket message type:** Add handler in `websocket_handler()` + `elif data.type === '...'` in frontend `ws.onmessage`.

**Testing a modified plugin on a Pi:** The plugin's install directory
(`~/meshpulse/plugins/<author>/<name>/`) is the source of truth; the web root copy
(`/var/www/html/meshpulse/plugins/<author>/<name>/`) is derived from it. Both
`plugin_manager` (on install) and `install.sh` (on every run) sync install dir ->
web root, one-way. So copy changed plugin files into the **install directory** and
re-run `./install.sh` — anything dropped straight into the web root survives only
until the next `install.sh`, which silently copies the old version back over it.

Copy the manifest too, not just the code: plugin asset URLs carry `?v=<version>`
read from the manifest, so a stale `plugin.json` means a stale URL and a cached
copy of the old JS.

**Debugging parsers:** Run directly, watch `[RECV]` lines, add print statements, check `journalctl -u meshpulse -f`.

## Testing

No automated tests. Manual:
```bash
python3 backend/meshpulse.py   # direct run
sudo systemctl restart meshpulse
sudo journalctl -u meshpulse -f
# browser console for frontend
```

## Known Issues & Limitations

- No authentication on WebSocket (trusted LAN only)
- MQTT nodes excluded from max range calculation
- Parser depends on meshtastic CLI output format — may break on CLI updates
- Safari requires `map.whenReady()` before initial data load
- Heltec V3 may need `--no-nodes` flag
- No rate limiting on WebSocket connections
- Serial traceroute pauses listener ~60s (USB port not shared)
- Open-Elevation API is free/public — may be slow or unavailable
