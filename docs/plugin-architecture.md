# Meshtastic Network Mapper — Plugin Architecture

**Status:** Foundation implemented in v2.2.0 (Phase 1). Full documentation to follow in Phase 2.

## Overview

The plugin system turns the Meshtastic Network Mapper into an extensible platform.
Plugins are ZIP archives (`.meshplugin`) that can add backend Python logic and/or
frontend UI components.

## Directory Structure

```
plugins/
  enabled.json          ← list of enabled plugin IDs
  {author}/
    {name}/
      plugin.json       ← manifest (required)
      main.py           ← backend entry point (optional)
      data.db           ← plugin's SQLite database (auto-created)
      config.json       ← user config overrides (auto-created on save)
      static/           ← frontend assets (JS, CSS, HTML snippets)

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
  "permissions": ["send_message", "read_nodes", "websocket"],
  "backend": {
    "entry_point": "main.py",
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
| `on_connect(info)` | Mapper connected to radio |
| `on_disconnect(reason)` | Mapper disconnected |
| `on_node_expire(info)` | Node TTL expired |
| `on_ws_client_connect(info)` | Browser connected |
| `on_ws_client_disconnect(info)` | Browser disconnected |

## WebSocket Protocol

Plugin data arrives at the frontend as:
```json
{ "type": "plugin_data", "plugin_id": "maxg10/bbs", "data": {...}, "channel": "plugin:maxg10/bbs:updates" }
```

## Full documentation

Full architecture specification will be added in Phase 2.
