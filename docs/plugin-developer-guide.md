# Plugin Developer Guide

Build your own plugins for MeshPulse. This guide walks you through creating, testing, and publishing a plugin from scratch.

## Table of Contents
1. [Overview](#overview)
2. [Plugin Structure](#plugin-structure)
3. [Step 1: Create the Manifest](#step-1-create-the-manifest)
4. [Step 2: Backend Plugin (Python)](#step-2-backend-plugin-python)
5. [Step 3: Frontend Plugin (JavaScript)](#step-3-frontend-plugin-javascript)
6. [Step 4: Combined Plugin (Backend + Frontend)](#step-4-combined-plugin)
7. [Step 5: Plugin Configuration](#step-5-plugin-configuration)
8. [Step 6: Testing Locally](#step-6-testing-locally)
9. [Step 7: Building the .meshplugin Package](#step-7-building-the-meshplugin-package)
10. [Step 8: Publishing](#step-8-publishing)
11. [API Reference](#api-reference)
12. [Examples](#examples)

## Overview

Plugins extend the mapper with new features. They can be:
- **Backend-only** — Python code that processes mesh data (e.g., MQTT Proxy)
- **Frontend-only** — JavaScript that adds UI elements to the map (e.g., Elevation Map)
- **Combined** — both backend and frontend working together

Plugins receive hooks for mesh events (messages, positions, telemetry, etc.) and can access the map, nodes, WebSocket, and more.

## Plugin Structure

```
my-plugin/
├── plugin.json          # Manifest (required)
├── requirements.txt     # Python dependencies (optional)
├── backend/
│   └── main.py          # Backend entry point (optional)
└── frontend/
    ├── js/
    │   └── plugin.js    # Frontend entry point (optional)
    └── css/
        └── plugin.css   # Styles (optional)
```

## Step 1: Create the Manifest

`plugin.json` is the only required file. It describes your plugin:

```json
{
    "id": "yourname/my-plugin",
    "name": "My Awesome Plugin",
    "version": "1.0.0",
    "description": "What your plugin does",
    "author": {
        "name": "Your Name",
        "github": "yourname",
        "url": "https://your-website.com"
    },
    "license": "GPL-3.0",
    "compatibility": {
        "min_mapper_version": "2.3.0",
        "tested_up_to": "2.3.0"
    },
    "permissions": [],
    "backend": {
        "entry_point": "backend/main.py",
        "requirements": "requirements.txt"
    },
    "frontend": {
        "js": "frontend/js/plugin.js",
        "css": "frontend/css/plugin.css"
    },
    "config": {
        "my_setting": {
            "type": "boolean",
            "default": true,
            "description": "Enable my feature"
        }
    }
}
```

### ID Format
Use `author/plugin-name` format (e.g., `maxg10/mqtt-proxy`). This must be unique.

### Permissions
Informational — tells users what the plugin accesses:
- `mesh_send` — can send messages to mesh
- `mesh_receive` — receives mesh packets
- `api_endpoints` — registers API routes
- `websocket_channel` — uses WebSocket channels
- `filesystem` — reads/writes files
- `database` — uses SQLite database
- `raw_map_access` — direct Leaflet map access
- `node_inject` — injects nodes from non-Meshtastic sources into the node store

### Config Types
- `boolean` — checkbox
- `number` — number input (with optional `min`, `max`)
- `string` — text input
- `select` — dropdown (with `options` array)

## Step 2: Backend Plugin (Python)

Create `backend/main.py`:

```python
from mapper.plugin_api import MeshPlugin

class MyPlugin(MeshPlugin):

    def on_enable(self):
        """Called when plugin is enabled. Set up resources here."""
        self.log("Plugin enabled!")

        # Access plugin config
        my_setting = self.config.get('my_setting', True)
        self.log(f"my_setting = {my_setting}")

        # Get a database connection (isolated per plugin)
        # db = self.get_database()
        # db.execute("CREATE TABLE IF NOT EXISTS ...")

    def on_disable(self):
        """Called when plugin is disabled. Clean up here."""
        self.log("Plugin disabled!")

    async def on_message(self, message):
        """New text message from mesh."""
        self.log(f"Message from {message['from_name']}: {message['text']}")

        # Reply to a command
        if message['text'].startswith('!hello'):
            await self.send_mesh_message(
                "Hello from my plugin!",
                to_id=message['from_id']
            )

    async def on_position(self, position):
        """GPS position update received."""
        self.log(f"Node {position['node_id']} at {position['lat']},{position['lon']}")

    async def on_telemetry(self, telemetry):
        """Telemetry data received."""
        pass

    async def on_node_update(self, node):
        """Node info updated."""
        pass
```

### Available Hooks

| Hook | When it fires | Arguments |
|------|--------------|-----------|
| `on_enable()` | Plugin enabled | — |
| `on_disable()` | Plugin disabled | — |
| `on_message(message)` | Text message received | `{from_id, from_name, to_id, text, timestamp, is_dm, channel_index}` |
| `on_node_update(node)` | Node info changed | `{id, name, lat, lon, alt, snr, role, hops, via_mqtt, ...}` |
| `on_position(position)` | GPS position update | `{node_id, lat, lon, alt, snr, rssi, hops, timestamp}` |
| `on_telemetry(telemetry)` | Telemetry packet | `{node_id, battery, voltage, channel_util, air_util_tx, ...}` |
| `on_neighborinfo(info)` | NeighborInfo update | `{node_id, neighbors: [{id, snr}, ...]}` |
| `on_mqtt_proxy(topic, data)` | MQTT proxy message | `topic` (str), `data` (bytes) |
| `on_connect(info)` | Mapper connected | `{connection_type, host_or_port, local_node_id}` |
| `on_disconnect(reason)` | Mapper disconnected | `{reason}` |
| `on_node_expire(info)` | Node TTL expired | `{node_id, last_seen}` |
| `on_ws_client_connect(info)` | Browser connected | `{client_id}` |
| `on_ws_client_disconnect(info)` | Browser disconnected | `{client_id}` |

### Available Methods

| Method | Description |
|--------|-------------|
| `self.log(message)` | Log with `[PLUGIN:id]` prefix |
| `self.get_database()` | Get isolated SQLite connection |
| `self.config` | Plugin config dict |
| `self.save_config()` | Save config to disk |
| `await self.send_mesh_message(text, to_id, channel)` | Send to mesh |
| `self.send_mqtt_to_device(topic, data)` | Send MQTT downlink |
| `self.inject_node(node_data)` | Inject a node from a non-Meshtastic source ([details](#node-injection)) |
| `self.get_tracker_config(section)` | Read tracker firmware config |
| `self.get_nodes()` | Get all nodes with GPS |
| `self.get_nodes_no_position()` | Get nodes without GPS |
| `self.get_node(node_id)` | Get specific node |
| `self.get_tracker_info()` | Get connected tracker info |
| `self.get_messages()` | Get recent messages |
| `await self.broadcast_ws(data, channel)` | Send to WebSocket clients |
| `self.register_ws_channel(name)` | Register WS channel |

### Python Dependencies

If your plugin needs Python packages, create `requirements.txt`:
```
paho-mqtt>=2.0.0
requests>=2.28.0
```

Dependencies are installed automatically when the plugin is enabled.

## Step 3: Frontend Plugin (JavaScript)

Create `frontend/js/plugin.js`:

```javascript
var MyPlugin = (function() {

    function Plugin() {
        this.api = null;
    }

    Plugin.prototype.onEnable = function(api) {
        this.api = api;
        console.log('[MyPlugin] Enabled!');

        // Add a control panel. Core owns the placement: top-left on desktop,
        // inside the mobile "Map Layers" drawer on phones. The fallback keeps the
        // plugin working on cores older than 2.6.1, which have no api.panels.
        var panel = document.createElement('div');
        panel.innerHTML = '<button id="my-btn">My Plugin</button>';
        panel.style.cssText = 'background:rgba(31,41,55,0.9);padding:6px 10px;border-radius:6px;color:#e5e7eb;font-size:12px;';
        this.panel = panel;
        if (api.panels && api.panels.register) api.panels.register(panel);
        else api.map.addControl('my-panel', panel, 'topleft');  // fallback < 2.6.1

        // Listen for node updates
        api.nodes.onUpdate(function(node) {
            console.log('Node updated:', node.id, node.name);
        });

        // Listen for messages
        api.messages.onMessage(function(msg) {
            console.log('Message:', msg.text);
        });

        // Access node data
        var allNodes = api.nodes.getAll();
        console.log('Total nodes:', Object.keys(allNodes).length);

        // Use namespaced storage (survives page refresh)
        api.storage.set('lastRun', Date.now());

        // Show notification
        api.ui.showNotification('My Plugin loaded!', 'success');
    };

    Plugin.prototype.onDisable = function(api) {
        // Clean up — remove panels, layers, listeners. Mirror the registration path.
        if (api.panels && api.panels.unregister) api.panels.unregister(this.panel);
        else api.map.removeControl('my-panel');
        console.log('[MyPlugin] Disabled');
    };

    Plugin.prototype.onConfigUpdate = function(newConfig) {
        // React to config changes without disable/enable
        console.log('Config changed:', newConfig);
    };

    return Plugin;
})();

// IMPORTANT: Export your class
window.MeshPlugin = MyPlugin;
```

### Frontend API (MapperAPI)

| API | Methods |
|-----|---------|
| `api.map` | `addLayer()`, `removeLayer()`, `addControl()`, `removeControl()`, `getLeafletMap()` |
| `api.panels` | `register(el)`, `unregister(el)` — preferred way to add a control panel (2.6.1+) |
| `api.nodes` | `getVisible()`, `getVisibleNoPosition()`, `getAll()`, `get(id)`, `getTracker()`, `getNoPosition()`, `onUpdate(cb)`, `onExpire(cb)`, `onFilterChange(cb)` |
| `api.messages` | `getAll()`, `onMessage(cb)`, `send(text, toId, channel)` |
| `api.ws` | `subscribe(channel, cb)`, `unsubscribe(channel)`, `send(channel, data)` |
| `api.ui` | `addNavItem(label, onClick)`, `addPanel(id, html, position)`, `showNotification(msg, type)` |
| `api.storage` | `get(key)`, `set(key, value)`, `remove(key)`, `getAll()` — auto-namespaced per plugin |
| `api.info` | `id`, `version`, `config`, `dataUrl(path)` |

### Control Panels (`api.panels`)

*Available since core 2.6.1.*

`api.panels` is the preferred way to put a control panel on screen. You hand the core a
plain DOM element and the core owns its placement:

- **Desktop** — the panel is hosted in `#plugin-panels`, floating at the top-left of the map.
- **Mobile** (≤768px) — the same DOM node is *moved* (not cloned) into the "Map Layers"
  drawer behind the 🗺️ FAB, so state and event listeners survive the move.

```javascript
// register(el) -> el   (adds the .plugin-panel class, tags the element with your plugin id)
var panel = document.createElement('div');
panel.innerHTML = '<label><input type="checkbox" id="my-toggle"> My layer</label>';

if (api.panels && api.panels.register) api.panels.register(panel);
else api.map.addControl('my-panel', panel, 'topleft');  // fallback < 2.6.1

// ...and on disable:
if (api.panels && api.panels.unregister) api.panels.unregister(panel);
else api.map.removeControl('my-panel');
```

**Always keep the fallback guard.** Users are not all on the newest core, and a plugin
that assumes `api.panels` exists renders nothing at all on 2.6.0 and older.

`api.map.addControl()` is *not* deprecated — it stays the escape hatch for real Leaflet
controls and for older cores. Cores from 2.6.1 on also adopt panels added through
`addControl()` into the mobile drawer automatically (matched on `.leaflet-plugin-control`),
so existing plugins keep working. But `api.panels` is explicit, needs no DOM-matching
heuristics, and is what new plugins should use.

### Visible vs. all nodes (`api.nodes.getVisible`)

*Available since core 2.7.0.*

The core's map has a filter chain — MQTT nodes, licensed only, Meshtastic/Meshcore,
direct only, unknown hops, routers only. `getAll()` returns the **unfiltered** node
list and knows nothing about it. If your plugin draws anything anchored to nodes —
labels, a heatmap, lines — rendering from `getAll()` means your overlay keeps drawing
nodes the user just filtered off the map. Uncheck "Meshtastic" and the markers vanish
while your labels float on an empty map.

Render from `getVisible()` and re-render on `onFilterChange()`:

```javascript
Plugin.prototype._render = function () {
    var api = this.api;
    // getVisible() is 2.7.0+; getAll() keeps the plugin working on older cores
    var nodes = api.nodes.getVisible ? api.nodes.getVisible() : api.nodes.getAll();
    // ...draw from `nodes`
};

Plugin.prototype.onEnable = function (api) {
    var self = this;
    this.api = api;
    api.nodes.onUpdate(function () { self._render(); });
    if (api.nodes.onFilterChange) api.nodes.onFilterChange(function () { self._render(); });
    this._render();
};
```

`onFilterChange(cb)` fires after the map has drawn, with the visible node array as its
argument, whenever the set may have changed — a filter toggled, nodes arrived, nodes
expired. One callback per plugin, same as `onUpdate`. `getVisibleNoPosition()` is the
same idea for nodes without GPS.

Keep `getAll()` for anything that genuinely needs the whole picture — counting the mesh,
looking up a node the user filtered out, exporting data.

### IMPORTANT Rules
- Always export: `window.MeshPlugin = YourClass;`
- Clean up in `onDisable()` — remove all layers, panels, controls, listeners
- Prefer `api.panels.register()` over `api.map.addControl()` for control panels, with a fallback guard for cores < 2.6.1
- Draw map overlays from `api.nodes.getVisible()`, not `getAll()`, and re-render on `api.nodes.onFilterChange()` — otherwise your overlay contradicts the map's own filters
- Use `api.storage` instead of `localStorage` directly (auto-namespaced)
- Use `api.map.getLeafletMap()` for direct Leaflet access (requires `raw_map_access` permission)
- All IDs are auto-prefixed with `plugin:author/name:` to avoid conflicts

## Step 4: Combined Plugin

A plugin can have both backend and frontend. They communicate via WebSocket channels:

**Backend (`backend/main.py`):**
```python
def on_enable(self):
    self.register_ws_channel('updates')

async def on_position(self, position):
    # Send data to frontend
    await self.broadcast_ws({
        'type': 'position_alert',
        'node': position['node_id'],
        'lat': position['lat'],
        'lon': position['lon']
    }, channel='updates')
```

**Frontend (`frontend/js/plugin.js`):**
```javascript
Plugin.prototype.onEnable = function(api) {
    api.ws.subscribe('updates', function(data) {
        if (data.type === 'position_alert') {
            api.ui.showNotification('Node moved: ' + data.node);
        }
    });
};
```

## Step 5: Plugin Configuration

Define config in `plugin.json`:
```json
"config": {
    "alert_enabled": {
        "type": "boolean",
        "default": true,
        "description": "Enable alerts"
    },
    "threshold": {
        "type": "number",
        "default": 50,
        "min": 1,
        "max": 100,
        "description": "Alert threshold (%)"
    },
    "mode": {
        "type": "select",
        "options": ["quiet", "normal", "verbose"],
        "default": "normal",
        "description": "Operation mode"
    }
}
```

The mapper auto-generates a settings UI. Users change values in Config → Plugins → Settings.

Backend receives live updates via `on_config_update(new_config)`.
Frontend receives live updates via `onConfigUpdate(newConfig)`.

## Step 6: Testing Locally

1. Create your plugin directory in the mapper's `plugins/` folder:
```bash
mkdir -p plugins/yourname/my-plugin
# Copy your files there
```

2. Restart the mapper:
```bash
sudo systemctl restart meshtastic-mapper
```

3. Open Config → Plugins → Enable your plugin

4. Check logs:
```bash
sudo journalctl -u meshtastic-mapper -f | grep PLUGIN
```

## Step 7: Building the .meshplugin Package

```bash
cd your-plugin-directory
zip -r my-plugin-1.0.0.meshplugin plugin.json backend/ frontend/ requirements.txt
```

The `.meshplugin` file is just a ZIP archive containing your plugin files.

## Step 8: Publishing

### Option A: GitHub Release (recommended)
1. Create a GitHub repo for your plugin (e.g., `yourname/meshplugin-my-plugin`)
2. Push your code
3. Create a release with the `.meshplugin` file attached
4. Contact us to add your plugin to the [Plugin Store](https://meshpulse.app/plugins)

### Option B: Manual Distribution
Share the `.meshplugin` file directly. Users install via Config → Plugins → Install Plugin (upload).

## API Reference

### Node Injection

`self.inject_node(node_data)` → `bool`

Insert or update a node from a **non-Meshtastic source** — another mesh protocol,
a bridge, an external data feed — into MeshPulse's main node store. Injected nodes
are first-class citizens: TTL cleanup, `nodes.json` persistence, WebSocket broadcast
and map rendering all apply automatically.

Declare the `node_inject` permission in `plugin.json` to use it.

**Required fields:**
- `id` (string, max 40 chars) — unique node ID
- `net` (string, max 8 chars, uppercased) — the network tag, e.g. `'MC'` for
  Meshcore. `'MT'` means Meshtastic and is the default for nodes with no `net` field.

**Optional fields:** `name`, `lat`, `lon`, `alt`, `role`, `snr`, `rssi`.
Any additional keys are passed through as-is and reach the frontend — useful for
protocol-specific fields.

**Position rules:** `lat`/`lon` must be valid floats in range; `(0, 0)` is treated
as "no position". Nodes without a position land in the no-GPS list. A later update
*without* a position does not wipe a previously known one.

**Return value:** `True` on success, `False` if the core rejects the data
(logged as `[INJECT] rejected: <reason>`).

**ID convention:** prefix IDs so they cannot collide with Meshtastic's `!xxxxxxxx` —
e.g. Meshcore uses `mc!` + first 8 hex chars of the node's public key.

**Frontend behaviour:** any node with `net != 'MT'` renders as a diamond marker,
and users can toggle whole networks on/off with the per-network checkboxes in
Mesh Info.

```python
self.inject_node({
    'id': 'mc!539f7e9e',
    'net': 'MC',
    'name': 'Triora - Sistel',
    'lat': 43.99161,
    'lon': 7.76807,
    'role': 'REPEATER',          # feeds the R/C counters
    'pubkey': '539f7e...fb30',   # extra field, passed through
    'mc_type': 'Repeater',       # extra field, shown in the popup
})
```

Full API reference: [Plugin Architecture Docs](plugin-architecture.md)

## Examples

- **Frontend-only:** [Elevation Map](https://github.com/maxg10/meshplugin-elevation-map) — map tile overlay
- **Backend-only:** [MQTT Proxy](https://github.com/maxg10/meshplugin-mqtt-proxy) — MQTT client proxy

## Getting Help

- [GitHub Issues](https://github.com/maxg10/meshtastic-network-mapper/issues)
- [Plugin Architecture Docs](plugin-architecture.md)
