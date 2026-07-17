# MeshPulse — Plugin Architecture

**Status:** Foundation implemented in v2.2.0 (Phase 1).

## Overview

The plugin system turns MeshPulse into an extensible platform.
Plugins are ZIP archives (`.meshplugin`) that can add backend Python logic and/or
frontend UI components.

## Directory Structure

```
plugins/
  enabled.json          ← list of enabled plugin IDs
  {author}/
    {name}/
      plugin.json       ← manifest (required)
      requirements.txt  ← Python dependencies (optional)
      backend/
        main.py         ← backend entry point (optional)
      frontend/
        js/plugin.js    ← frontend entry point (optional)
        css/plugin.css  ← styles (optional)
      data.db           ← plugin's SQLite database (auto-created)
      config.json       ← user config overrides (auto-created on save)

mapper/
  __init__.py
  plugin_api.py         ← MeshPlugin base class
  plugin_manager.py     ← PluginManager lifecycle management
```

## Plugin Manifest (plugin.json)

```json
{
  "id": "maxg10/bbs",
  "name": "BBS / Message Board",
  "version": "1.0.0",
  "description": "Bulletin board system for Meshtastic networks",
  "author": { "name": "Max Gieparda", "github": "maxg10" },
  "license": "GPL-3.0",
  "compatibility": { "min_mapper_version": "2.2.0" },
  "permissions": ["mesh_receive", "mesh_send"],
  "backend": {
    "entry_point": "backend/main.py",
    "requirements": "requirements.txt"
  },
  "frontend": {
    "tab": { "label": "BBS", "icon": "💬", "page": "static/bbs.html" }
  },
  "config": {
    "max_messages": { "type": "int", "default": 100, "label": "Max stored messages" }
  }
}
```

## Available Hooks

| Hook | Trigger |
|------|---------|
| `on_enable()` | Plugin enabled |
| `on_disable()` | Plugin disabled |
| `on_message(message)` | Text message received |
| `on_node_update(node)` | Node info updated |
| `on_position(position)` | GPS position received |
| `on_telemetry(telemetry)` | Telemetry packet received |
| `on_neighborinfo(neighborinfo)` | NeighborInfo received |
| `on_mqtt_proxy(topic, data)` | MQTT proxy message from device |
| `on_connect(info)` | Mapper connected to radio |
| `on_disconnect(reason)` | Mapper disconnected |
| `on_node_expire(info)` | Node TTL expired |
| `on_ws_client_connect(info)` | Browser connected |
| `on_ws_client_disconnect(info)` | Browser disconnected |

## Plugin API Methods

Plugins can call these methods via `self.method_name()` in backend code:

| Method | Description |
|--------|-------------|
| `get_database()` | Get plugin's isolated SQLite connection (`data.db`) |
| `log(message)` | Print with `[PLUGIN:id]` prefix |
| `send_mesh_message(text, to_id, channel)` | Send text to mesh (async) |
| `send_mqtt_to_device(topic, data)` | Send MQTT downlink to tracker |
| `inject_node(node_data)` | Insert/update a node from a non-Meshtastic source (requires `node_inject` permission) |
| `get_tracker_config(section)` | Read tracker firmware config (mqtt, lora, device, etc.) |
| `broadcast_ws(data, channel)` | Broadcast to WebSocket clients (async) |
| `register_ws_channel(channel_name)` | Register a plugin WS channel |
| `get_nodes()` | Get all nodes with GPS |
| `get_nodes_no_position()` | Get all nodes without GPS |
| `get_node(node_id)` | Get specific node data |
| `get_tracker_info()` | Get connected tracker info |
| `get_messages()` | Get recent messages |
| `get_config()` | Get plugin config dict |
| `save_config()` | Save plugin config to disk |

## MQTT Client Proxy Support

Plugins can act as MQTT client proxies for trackers without WiFi.

When a tracker has `proxy_to_client_enabled: True` in its MQTT config,
it sends `mqttClientProxyMessage` events to the connected client.
The mapper catches these and dispatches the `on_mqtt_proxy` hook.

**Reading tracker config:**
```python
mqtt_config = self.get_tracker_config('mqtt')
# Returns: {'enabled': True, 'address': 'loranet.pl', 'root': 'msh/PL', ...}
```

**Sending MQTT downlink:**
```python
self.send_mqtt_to_device(topic, payload_bytes)
# Builds ToRadio.mqttClientProxyMessage and sends to device
```

## WebSocket Protocol

Plugin data arrives at the frontend as:
```json
{ "type": "plugin_data", "plugin_id": "maxg10/bbs", "data": {...}, "channel": "plugin:maxg10/bbs:updates" }
```
