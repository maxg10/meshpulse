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

**Debugging parsers:** Run directly, watch `[RECV]` lines, add print statements, check `journalctl -u meshtastic-mapper -f`.

## Testing

No automated tests. Manual:
```bash
python3 backend/meshtastic_mapper.py   # direct run
sudo systemctl restart meshtastic-mapper
sudo journalctl -u meshtastic-mapper -f
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
