# AGENTS.md

Instructions for AI coding agents working on MeshPulse. `CLAUDE.md` is a symlink
to this file — one document, two names, so the two cannot drift apart.

**Author:** Mariusz "Max" Gieparda | mgieparda@yahoo.com | github.com/maxg10
**License:** GPL-3.0 · **Current version:** v2.7.1

## What this is

MeshPulse is a protocol-agnostic mapper and plugin platform for LoRa mesh
networks. It connects to a Meshtastic node over USB serial or TCP, renders the
mesh on a Leaflet map in real time, and exposes a WordPress-style plugin system.
Meshcore nodes appear on the same map through a plugin, not through core code.

Target hardware is a Raspberry Pi (Model B+ and up, 512 MB RAM) — keep
dependencies and memory use modest.

## Layout

| What | Where |
|---|---|
| Backend | `backend/meshpulse.py` (~5.8k lines, class `ListenBasedMapper`) |
| Plugin API (public) | `mapper/plugin_api.py` — `MeshPlugin` base class |
| Plugin loader | `mapper/plugin_manager.py` |
| Frontend | `frontend/{index,config,stats,messages}.html` + `styles.css` |
| Plugin store manifest | `plugins-store/plugins.json` (deployed to meshpulse.app) |
| Plugin install dir | `plugins/<author>/<name>/` (gitignored, runtime) |
| Web root | `/var/www/html/meshpulse/` |
| WebSocket | port `8765` |
| Data files | `nodes.json`, `stats.db`, `config.json` — all in the web root |
| Service | systemd unit `meshpulse` |

Plugins live in a separate repository: **`github.com/maxg10/meshplugins`**, a
monorepo holding all five plugins (bbs, elevation-map, meshcore, mqtt-proxy,
weather-overlay), each released with its own tag and `.meshplugin` package.

## Architecture in one screen

- The backend spawns `meshtastic --listen` as a subprocess and parses its stdout;
  `TCPMeshtasticInterface` and `SerialMeshtasticInterface` wrap the two transports.
- Node state lives in two stores: `self.nodes` (with GPS) and
  `self.nodes_no_position`. Both are written to `nodes.json` and broadcast over
  the WebSocket.
- The frontend is vanilla JS with everything inline in the HTML — no build step,
  no bundler, no framework. WebSocket is the primary channel; polling
  `nodes.json` is the fallback.
- Plugins are loaded once per process after the mapper is constructed, and
  survive USB↔TCP reconnects untouched (`_plugins_loaded` one-shot flag).

Details in `docs/architecture.md` and `docs/plugin-architecture.md`.

## Plugin API surface (public — do not break)

Backend hooks are documented in `mapper/plugin_api.py`; the frontend API is built
in `createMapperAPI()` in `frontend/index.html`. Since 2.6.1:

- `api.panels.register(el)` / `unregister(el)` — control panels; core owns
  placement (top-left on desktop, mobile drawer on phones)
- `api.nodes.getVisible()` / `getVisibleNoPosition()` / `onFilterChange(cb)` —
  the node set the map is actually showing, after the core's filter chain.
  Anything drawn on the map must use these, not `getAll()`
- `frontend.pages[]` in a manifest renders navbar tabs, no plugin JS required
- `on_ws_request(data, channel, reply)` — answer the browser that asked, instead
  of broadcasting to all of them

`register_api_route()` exists but is **not implemented** — the routes are stored
and never dispatched, because there is no HTTP application server. Use a
WebSocket channel.

New APIs are additive and plugins guard them
(`api.nodes.getVisible ? … : getAll()`), so older cores keep working.

## Conventions

**Python:** snake_case functions, PascalCase classes, `print()` for logging,
dict-based storage, async WebSocket handlers.
**JavaScript:** vanilla, camelCase, global state at the top of the inline
`<script>`, exponential backoff on reconnect.
**CSS:** `frontend/styles.css` only. Node colors: green `#22c55e`, yellow
`#eab308`, red `#ef4444`, blue `#3b82f6`.
**Language:** all code, comments, documentation and UI strings in English.

No test suite, no linter, no build process. Verify frontend changes by extracting
the inline `<script>` blocks and running `node --check`; verify Python with
`python3 -m py_compile`.

## Traps that have cost real time

- **A plugin's install directory is canonical; the web root copy is derived.**
  `plugin_manager` (on install) and `install.sh` (on every run) sync install dir
  → web root, one way. Files dropped straight into the web root are silently
  overwritten with the old version on the next `install.sh`.
- **Frontend changes need `./install.sh` on a Pi** — `git pull` alone only
  updates the backend.
- **Plugin asset URLs carry `?v=<manifest version>`**, so copy `plugin.json`
  along with the code or the browser keeps serving the cached old file.
- **Serial config writes deadlock** (`writeConfig()` → `ensureSessionKey()` →
  `requestConfig()` fights the listener). Use a subprocess CLI call instead.
- **WebSocket must use `compression=None`** — `permessage-deflate` corrupts
  buffers on slower Pis.
- **Esri tiles use `{z}/{y}/{x}`**, not `{z}/{x}/{y}` like OSM.
- **The store can be ahead of the repo** — `plugins.json` has been edited
  directly on the server before. Check the live file before assuming.
- **Plugin release tags use a hyphen** (`weather-overlay-v1.0.3`), never a
  slash — a slash becomes `%2F` in the download URL.
- **`gh release create` always with `--notes-file`**, never inline: bash eats `!`
  and clipboard mangles heredocs.
- **The WebSocket port is hardcoded to 8765 in the frontend**, so remapping it on
  the host does not work.
- Docker has no Bluetooth and no USB serial passthrough — TCP only.

## Release process

```
work on dev → merge --no-ff into main → annotated tag vX.Y.Z
→ docker buildx (linux/amd64,arm64,arm/v7, --sbom=true --provenance=mode=max)
→ gh release create --notes-file
```

Release notes must end with `## Updating`, `## Docker`, `## Fresh Install`, in
that order, each with fenced code blocks. The SBOM workflow fires automatically
on `release: published`.

Deployment to a Pi is `git fetch && git reset --hard origin/dev` (the Pi only
consumes code and carries runtime files), then `./install.sh`, then
`sudo systemctl restart meshpulse`.

## Docs

| File | Covers |
|---|---|
| `docs/architecture.md` | backend, frontend, WebSocket, LOS, StatsDB |
| `docs/plugin-architecture.md` | how the plugin system is put together |
| `docs/plugin-developer-guide.md` | writing a plugin — start here |
| `docs/deployment.md` | Docker, systemd, dependencies |
| `docs/development.md` | conventions, common tasks, known issues |
| `docs/changelog.md` | full version history |
