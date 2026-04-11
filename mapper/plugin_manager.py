# Meshtastic Network Mapper — Plugin Manager
# Copyright (C) 2025-2026 Mariusz "Max" Gieparda
# Licensed under GPL-3.0 — see LICENSE file

"""
PluginManager — handles plugin lifecycle:
install, enable, disable, uninstall, hook dispatch.
"""

import os
import sys
import json
import shutil
import zipfile
import asyncio
import importlib
import importlib.util
import time
import subprocess
from mapper.plugin_api import MeshPlugin


class PluginManager:
    """Manages plugin lifecycle and hook dispatch."""

    def __init__(self, mapper, plugins_dir=None, mapper_version='0.0.0'):
        """Initialize PluginManager.

        Args:
            mapper: ListenBasedMapper instance
            plugins_dir (str): Path to plugins directory.
                Defaults to 'plugins/' relative to the repo root.
            mapper_version (str): Current mapper version for compatibility checks.
        """
        self.mapper = mapper
        self.mapper_version = mapper_version

        # Determine plugins directory — next to backend/ in the repo
        if plugins_dir:
            self.plugins_dir = plugins_dir
        else:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self.plugins_dir = os.path.join(repo_root, 'plugins')

        self.enabled_file = os.path.join(self.plugins_dir, 'enabled.json')
        self.plugins = {}           # {plugin_id: MeshPlugin instance}
        self.enabled_ids = []       # List of enabled plugin IDs
        self.ws_channels = {}       # {channel_name: plugin_id}
        self.api_routes = {}        # {(method, path): {'plugin_id': ..., 'handler': ...}}

        # Reference to connected_clients set — set from meshtastic_mapper after init
        self._connected_clients = None

        # Ensure plugins directory exists
        os.makedirs(self.plugins_dir, exist_ok=True)

        # Load enabled list
        self._load_enabled_list()
        print(f"[PLUGINS] Plugin directory: {self.plugins_dir}")
        print(f"[PLUGINS] Enabled plugins: {self.enabled_ids}")

    # ── Enabled List Persistence ────────────────────────────────

    def _load_enabled_list(self):
        """Load list of enabled plugin IDs from enabled.json."""
        try:
            if os.path.exists(self.enabled_file):
                with open(self.enabled_file, 'r') as f:
                    self.enabled_ids = json.load(f)
        except Exception as e:
            print(f"[PLUGINS] Error loading enabled list: {e}")
            self.enabled_ids = []

    def _save_enabled_list(self):
        """Save list of enabled plugin IDs to enabled.json."""
        try:
            with open(self.enabled_file, 'w') as f:
                json.dump(self.enabled_ids, f, indent=2)
        except Exception as e:
            print(f"[PLUGINS] Error saving enabled list: {e}")

    # ── Plugin Discovery ────────────────────────────────────────

    def _get_plugin_dir(self, plugin_id):
        """Get the directory path for a plugin.

        Args:
            plugin_id (str): e.g. "maxg10/bbs"

        Returns:
            str: Path like plugins/maxg10/bbs/
        """
        parts = plugin_id.split('/', 1)
        if len(parts) == 2:
            return os.path.join(self.plugins_dir, parts[0], parts[1])
        return os.path.join(self.plugins_dir, plugin_id)

    def _read_manifest(self, plugin_dir):
        """Read and validate plugin.json from a plugin directory.

        Args:
            plugin_dir (str): Path to plugin directory

        Returns:
            dict or None: Parsed manifest, or None if invalid
        """
        manifest_path = os.path.join(plugin_dir, 'plugin.json')
        if not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, 'r') as f:
                manifest = json.load(f)
            required = ['id', 'name', 'version', 'compatibility']
            for field in required:
                if field not in manifest:
                    print(f"[PLUGINS] Manifest missing field: {field}")
                    return None
            return manifest
        except Exception as e:
            print(f"[PLUGINS] Error reading manifest: {e}")
            return None

    def _load_plugin_config(self, plugin_dir, manifest):
        """Load plugin config: merge manifest defaults with user overrides.

        Args:
            plugin_dir (str): Path to plugin directory
            manifest (dict): Plugin manifest

        Returns:
            dict: Merged config values
        """
        config = {}
        manifest_config = manifest.get('config', {})
        for key, spec in manifest_config.items():
            config[key] = spec.get('default')

        user_config_path = os.path.join(plugin_dir, 'config.json')
        if os.path.exists(user_config_path):
            try:
                with open(user_config_path, 'r') as f:
                    user_config = json.load(f)
                config.update(user_config)
            except Exception as e:
                print(f"[PLUGINS] Error loading user config: {e}")

        return config

    # ── Installation ────────────────────────────────────────────

    def install(self, meshplugin_path):
        """Install plugin from .meshplugin file.

        Args:
            meshplugin_path (str): Path to .meshplugin file (ZIP archive)

        Returns:
            dict: {success, plugin_id, name, version, error?}
        """
        try:
            if not zipfile.is_zipfile(meshplugin_path):
                return {'success': False, 'error': 'Not a valid .meshplugin file (not a ZIP)'}

            with zipfile.ZipFile(meshplugin_path, 'r') as zf:
                names = zf.namelist()
                if 'plugin.json' not in names:
                    return {'success': False, 'error': 'No plugin.json found in package'}

                manifest = json.loads(zf.read('plugin.json'))
                plugin_id = manifest.get('id')
                if not plugin_id or '/' not in plugin_id:
                    return {'success': False, 'error': 'Invalid plugin ID (must be author/name)'}

                # Check compatibility
                min_version = manifest.get('compatibility', {}).get('min_mapper_version', '0.0.0')
                if not self._version_gte(self.mapper_version, min_version):
                    return {
                        'success': False,
                        'error': f'Plugin requires mapper >= {min_version}, current: {self.mapper_version}'
                    }

                plugin_dir = self._get_plugin_dir(plugin_id)

                if os.path.exists(plugin_dir):
                    if plugin_id in self.enabled_ids:
                        self.disable(plugin_id)
                    shutil.rmtree(plugin_dir)

                os.makedirs(plugin_dir, exist_ok=True)
                zf.extractall(plugin_dir)

                # Install Python dependencies if requirements.txt exists
                backend_config = manifest.get('backend')
                if backend_config:
                    req_file = backend_config.get('requirements')
                    if req_file:
                        req_path = os.path.join(plugin_dir, req_file)
                        if os.path.exists(req_path):
                            print(f"[PLUGINS] Installing dependencies from {req_file}...")
                            try:
                                subprocess.check_call([
                                    sys.executable, '-m', 'pip', 'install',
                                    '-r', req_path,
                                    '--break-system-packages',
                                    '--quiet'
                                ])
                                print(f"[PLUGINS] Dependencies installed")
                            except subprocess.CalledProcessError as e:
                                print(f"[PLUGINS] Warning: pip install failed: {e}")

                print(f"[PLUGINS] Installed {plugin_id} v{manifest.get('version')}")
                return {
                    'success': True,
                    'plugin_id': plugin_id,
                    'name': manifest.get('name'),
                    'version': manifest.get('version')
                }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def install_from_upload(self, filename, base64_data):
        """Install plugin from base64-encoded upload data.

        Args:
            filename (str): Original filename
            base64_data (str): Base64-encoded file content

        Returns:
            dict: {success, plugin_id, name, version, error?}
        """
        import base64
        try:
            data = base64.b64decode(base64_data)
            temp_path = os.path.join(self.plugins_dir, f'_upload_{filename}')
            with open(temp_path, 'wb') as f:
                f.write(data)

            result = self.install(temp_path)

            try:
                os.remove(temp_path)
            except Exception:
                pass

            return result
        except Exception as e:
            return {'success': False, 'error': f'Upload error: {str(e)}'}

    # ── Enable / Disable ────────────────────────────────────────

    def enable(self, plugin_id):
        """Enable an installed plugin.

        Loads the Python module, instantiates the plugin class,
        calls on_enable(), and starts receiving hooks.

        Args:
            plugin_id (str): Plugin ID e.g. "maxg10/bbs"

        Returns:
            dict: {success, plugin_id, error?}
        """
        try:
            if plugin_id in self.plugins:
                return {'success': True, 'plugin_id': plugin_id, 'message': 'Already enabled'}

            plugin_dir = self._get_plugin_dir(plugin_id)
            manifest = self._read_manifest(plugin_dir)
            if not manifest:
                return {'success': False, 'plugin_id': plugin_id, 'error': 'Cannot read plugin.json'}

            config = self._load_plugin_config(plugin_dir, manifest)

            backend_config = manifest.get('backend')
            plugin_instance = None

            if backend_config and backend_config.get('entry_point'):
                entry_point = os.path.join(plugin_dir, backend_config['entry_point'])
                if not os.path.exists(entry_point):
                    return {'success': False, 'plugin_id': plugin_id,
                            'error': f'Entry point not found: {backend_config["entry_point"]}'}

                module_name = f'meshplugin_{plugin_id.replace("/", "_")}'
                spec = importlib.util.spec_from_file_location(module_name, entry_point)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                plugin_class = None
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and
                            issubclass(attr, MeshPlugin) and
                            attr is not MeshPlugin):
                        plugin_class = attr
                        break

                if not plugin_class:
                    return {'success': False, 'plugin_id': plugin_id,
                            'error': 'No MeshPlugin subclass found in entry point'}

                plugin_instance = plugin_class()
            else:
                # Frontend-only plugin
                plugin_instance = MeshPlugin()

            plugin_instance.plugin_id = plugin_id
            plugin_instance.plugin_dir = plugin_dir
            plugin_instance.config = config
            plugin_instance._mapper = self.mapper
            plugin_instance._manager = self
            plugin_instance._manifest = manifest

            try:
                plugin_instance.on_enable()
            except Exception as e:
                print(f"[PLUGINS] {plugin_id} on_enable error: {e}")
                return {'success': False, 'plugin_id': plugin_id,
                        'error': f'on_enable failed: {str(e)}'}

            self.plugins[plugin_id] = plugin_instance
            if plugin_id not in self.enabled_ids:
                self.enabled_ids.append(plugin_id)
                self._save_enabled_list()

            print(f"[PLUGINS] Enabled: {plugin_id} v{manifest.get('version')}")
            return {'success': True, 'plugin_id': plugin_id}

        except Exception as e:
            print(f"[PLUGINS] Error enabling {plugin_id}: {e}")
            return {'success': False, 'plugin_id': plugin_id, 'error': str(e)}

    def disable(self, plugin_id):
        """Disable a running plugin.

        Calls on_disable(), detaches hooks, removes API routes and WS channels.

        Args:
            plugin_id (str): Plugin ID

        Returns:
            dict: {success, plugin_id, error?}
        """
        try:
            plugin = self.plugins.get(plugin_id)
            if not plugin:
                return {'success': True, 'plugin_id': plugin_id, 'message': 'Not enabled'}

            try:
                plugin.on_disable()
            except Exception as e:
                print(f"[PLUGINS] {plugin_id} on_disable error: {e}")

            channels_to_remove = [ch for ch, pid in self.ws_channels.items() if pid == plugin_id]
            for ch in channels_to_remove:
                del self.ws_channels[ch]

            routes_to_remove = [key for key, val in self.api_routes.items() if val['plugin_id'] == plugin_id]
            for key in routes_to_remove:
                del self.api_routes[key]

            del self.plugins[plugin_id]

            if plugin_id in self.enabled_ids:
                self.enabled_ids.remove(plugin_id)
                self._save_enabled_list()

            module_name = f'meshplugin_{plugin_id.replace("/", "_")}'
            if module_name in sys.modules:
                del sys.modules[module_name]

            print(f"[PLUGINS] Disabled: {plugin_id}")
            return {'success': True, 'plugin_id': plugin_id}

        except Exception as e:
            print(f"[PLUGINS] Error disabling {plugin_id}: {e}")
            return {'success': False, 'plugin_id': plugin_id, 'error': str(e)}

    # ── Uninstall ───────────────────────────────────────────────

    def uninstall(self, plugin_id):
        """Uninstall a plugin completely.

        Disables if enabled, deletes plugin directory.

        Args:
            plugin_id (str): Plugin ID

        Returns:
            dict: {success, plugin_id, error?}
        """
        try:
            if plugin_id in self.plugins:
                self.disable(plugin_id)

            plugin_dir = self._get_plugin_dir(plugin_id)
            if os.path.exists(plugin_dir):
                shutil.rmtree(plugin_dir)
                print(f"[PLUGINS] Uninstalled: {plugin_id}")

                author_dir = os.path.dirname(plugin_dir)
                if os.path.exists(author_dir) and not os.listdir(author_dir):
                    os.rmdir(author_dir)
            else:
                print(f"[PLUGINS] Plugin directory not found: {plugin_dir}")

            if plugin_id in self.enabled_ids:
                self.enabled_ids.remove(plugin_id)
                self._save_enabled_list()

            return {'success': True, 'plugin_id': plugin_id}

        except Exception as e:
            return {'success': False, 'plugin_id': plugin_id, 'error': str(e)}

    # ── Hook Dispatch ───────────────────────────────────────────

    async def dispatch_hook(self, hook_name, data):
        """Call a hook on all enabled plugins that implement it.

        Uses asyncio.gather with return_exceptions=True so one failing
        plugin doesn't block others.

        Args:
            hook_name (str): Hook method name e.g. 'on_message'
            data (dict): Data to pass to the hook
        """
        if not self.plugins:
            return

        tasks = []
        for plugin_id, plugin in self.plugins.items():
            method = getattr(plugin, hook_name, None)
            if method and callable(method):
                # Only dispatch if the method is actually overridden
                if type(plugin).__dict__.get(hook_name) is not None:
                    tasks.append(self._safe_call(plugin_id, hook_name, method, data))

        if tasks:
            await asyncio.gather(*tasks)

    async def _safe_call(self, plugin_id, hook_name, method, data):
        """Call a plugin hook method with exception handling."""
        try:
            await method(data)
        except Exception as e:
            print(f"[PLUGIN:{plugin_id}] Error in {hook_name}: {e}")

    def dispatch_hook_sync(self, hook_name, data):
        """Synchronous wrapper for dispatch_hook.

        Used in parsers alongside asyncio.run() broadcasts.
        Schedules the hook dispatch without creating a nested event loop.

        Args:
            hook_name (str): Hook method name
            data (dict): Data to pass to the hook
        """
        if not self.plugins:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.dispatch_hook(hook_name, data))
            else:
                loop.run_until_complete(self.dispatch_hook(hook_name, data))
        except RuntimeError:
            asyncio.run(self.dispatch_hook(hook_name, data))

    # ── WebSocket Channels ──────────────────────────────────────

    def register_ws_channel(self, plugin_id, channel_name):
        """Register a WebSocket channel for a plugin."""
        full_name = f'plugin:{plugin_id}:{channel_name}'
        self.ws_channels[full_name] = plugin_id
        print(f"[PLUGINS] Registered WS channel: {full_name}")

    async def broadcast_plugin_ws(self, plugin_id, data, channel=None):
        """Broadcast data to WebSocket clients from a plugin."""
        import websockets as ws_lib

        clients = self._connected_clients
        if not clients:
            return

        msg = {
            'type': 'plugin_data',
            'plugin_id': plugin_id,
            'data': data
        }
        if channel:
            msg['channel'] = f'plugin:{plugin_id}:{channel}'

        try:
            message_str = json.dumps(msg, ensure_ascii=False)
            message_str.encode('utf-8')
        except (UnicodeEncodeError, ValueError):
            message_str = json.dumps(msg, ensure_ascii=True)

        ws_lib.broadcast(set(clients), message_str)

    # ── API Routes ──────────────────────────────────────────────

    def register_api_route(self, plugin_id, method, path, handler):
        """Register an API route for a plugin."""
        full_path = f'/api/plugins/{plugin_id}{path}'
        self.api_routes[(method.upper(), full_path)] = {
            'plugin_id': plugin_id,
            'handler': handler
        }
        print(f"[PLUGINS] Registered API route: {method.upper()} {full_path}")

    async def handle_api_request(self, method, path, data=None):
        """Handle an API request to a plugin route."""
        route = self.api_routes.get((method.upper(), path))
        if route:
            try:
                return await route['handler'](data or {})
            except Exception as e:
                print(f"[PLUGINS] API error {method} {path}: {e}")
                return {'error': str(e)}
        return None

    # ── Mesh Message Sending ────────────────────────────────────

    async def send_mesh_message(self, text, to_id=None, channel=0):
        """Send a mesh message on behalf of a plugin."""
        mapper = self.mapper
        if not mapper:
            return

        dest = to_id if to_id else '^all'
        loop = asyncio.get_event_loop()

        if mapper.connection_type == 'tcp' and mapper.host:
            def _tcp_send():
                import meshtastic.tcp_interface
                iface = meshtastic.tcp_interface.TCPInterface(hostname=mapper.host, noProto=False)
                try:
                    iface.sendText(text, channelIndex=channel, destinationId=dest)
                finally:
                    try:
                        iface.close()
                    except Exception:
                        pass
            await loop.run_in_executor(None, _tcp_send)
        elif hasattr(mapper, '_serial_iface') and mapper._serial_iface:
            def _serial_send():
                mapper._serial_iface.sendText(text, channelIndex=channel, destinationId=dest)
            await loop.run_in_executor(None, _serial_send)

    # ── Plugin Listing ──────────────────────────────────────────

    def list_plugins(self):
        """List all installed plugins with their status.

        Scans the plugins directory for plugin.json files.

        Returns:
            list: List of plugin info dicts
        """
        plugins = []

        if not os.path.exists(self.plugins_dir):
            return plugins

        for author in os.listdir(self.plugins_dir):
            author_dir = os.path.join(self.plugins_dir, author)
            if not os.path.isdir(author_dir) or author.startswith('_') or author == 'enabled.json':
                continue
            for name in os.listdir(author_dir):
                plugin_dir = os.path.join(author_dir, name)
                if not os.path.isdir(plugin_dir):
                    continue
                manifest = self._read_manifest(plugin_dir)
                if manifest:
                    plugin_id = manifest['id']
                    plugins.append({
                        'id': plugin_id,
                        'name': manifest.get('name', name),
                        'version': manifest.get('version', '0.0.0'),
                        'description': manifest.get('description', ''),
                        'author': manifest.get('author', {}),
                        'license': manifest.get('license', ''),
                        'enabled': plugin_id in self.enabled_ids,
                        'permissions': manifest.get('permissions', []),
                        'compatibility': manifest.get('compatibility', {}),
                        'has_backend': manifest.get('backend') is not None,
                        'has_frontend': manifest.get('frontend') is not None,
                        'frontend': manifest.get('frontend'),
                        'config_schema': manifest.get('config', {}),
                    })

        return plugins

    def get_plugin_info(self, plugin_id):
        """Get detailed info about a specific plugin."""
        plugin_dir = self._get_plugin_dir(plugin_id)
        manifest = self._read_manifest(plugin_dir)
        if not manifest:
            return None

        return {
            'id': plugin_id,
            'manifest': manifest,
            'enabled': plugin_id in self.enabled_ids,
            'config': self._load_plugin_config(plugin_dir, manifest),
            'has_database': os.path.exists(os.path.join(plugin_dir, 'data.db')),
        }

    def save_plugin_config(self, plugin_id, config_updates):
        """Save plugin config updates."""
        try:
            plugin_dir = self._get_plugin_dir(plugin_id)
            config_path = os.path.join(plugin_dir, 'config.json')

            existing = {}
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    existing = json.load(f)

            existing.update(config_updates)

            with open(config_path, 'w') as f:
                json.dump(existing, f, indent=2)

            if plugin_id in self.plugins:
                self.plugins[plugin_id].config.update(config_updates)

            return {'success': True, 'plugin_id': plugin_id}
        except Exception as e:
            return {'success': False, 'plugin_id': plugin_id, 'error': str(e)}

    # ── Auto-Enable on Startup ──────────────────────────────────

    def load_enabled_plugins(self):
        """Enable all plugins that were enabled before mapper restart.

        Called once at mapper startup.
        """
        for plugin_id in list(self.enabled_ids):
            plugin_dir = self._get_plugin_dir(plugin_id)
            if os.path.exists(os.path.join(plugin_dir, 'plugin.json')):
                result = self.enable(plugin_id)
                if not result.get('success'):
                    print(f"[PLUGINS] Failed to auto-enable {plugin_id}: {result.get('error')}")
            else:
                print(f"[PLUGINS] Plugin {plugin_id} not found — removing from enabled list")
                self.enabled_ids.remove(plugin_id)
        self._save_enabled_list()

    # ── Utility ─────────────────────────────────────────────────

    @staticmethod
    def _version_gte(current, minimum):
        """Check if current version >= minimum version.

        Simple semver comparison (major.minor.patch).
        """
        def parse(v):
            v = v.split('-')[0]
            parts = v.split('.')
            return tuple(int(p) for p in parts[:3])
        try:
            return parse(current) >= parse(minimum)
        except (ValueError, IndexError):
            return True  # If we can't parse, allow it
