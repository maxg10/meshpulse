// Elevation Map Plugin for Meshtastic Network Mapper
// Copyright (C) 2025-2026 Mariusz "Max" Gieparda
// Licensed under GPL-3.0 — see LICENSE file

var ElevationMapPlugin = (function() {

    function ElevationMap() {
        this.api = null;
        this.tileLayer = null;
        this._originalBase = null;
        this._currentProvider = null;
        this._onStorage = null;
    }

    var providers = {
        opentopomap: {
            url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
            attribution: '&copy; OpenTopoMap (CC-BY-SA)',
            maxZoom: 17
        },
        stamen_terrain: {
            url: 'https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}.png',
            attribution: '&copy; Stadia Maps / Stamen Terrain',
            maxZoom: 18
        },
        shaded_relief: {
            url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}',
            attribution: '&copy; Esri World Shaded Relief',
            maxZoom: 13
        }
    };

    ElevationMap.prototype.onEnable = function(api) {
        this.api = api;

        // Read provider from localStorage (live changes) or config (defaults)
        var storageKey = 'plugin:' + api.info.id + ':tile_provider';
        var providerKey = localStorage.getItem(storageKey) || api.info.config.tile_provider || 'opentopomap';
        var provider = providers[providerKey] || providers.opentopomap;

        this.tileLayer = L.tileLayer(provider.url, {
            maxZoom: provider.maxZoom,
            attribution: provider.attribution
        });
        this._currentProvider = providerKey;

        // Build checkbox control
        var control = document.createElement('div');
        control.style.cssText = 'background:rgba(31,41,55,0.9);padding:6px 10px;border-radius:6px;color:#e5e7eb;font-size:12px;box-shadow:0 2px 6px rgba(0,0,0,0.3);';
        control.innerHTML =
            '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;white-space:nowrap">' +
                '<input type="checkbox" id="plugin-elev-toggle"' +
                (api.info.config.default_enabled ? ' checked' : '') + '>' +
                '<span>\u26f0\ufe0f Elevation Map</span>' +
            '</label>';

        // Swap base layer on toggle
        var self = this;
        control.querySelector('#plugin-elev-toggle').addEventListener('change', function(e) {
            var rawMap = api.map.getLeafletMap();
            if (e.target.checked) {
                rawMap.eachLayer(function(layer) {
                    if (layer instanceof L.TileLayer && layer !== self.tileLayer) {
                        self._originalBase = layer;
                        rawMap.removeLayer(layer);
                    }
                });
                self.tileLayer.addTo(rawMap);
            } else {
                rawMap.removeLayer(self.tileLayer);
                if (self._originalBase) {
                    self._originalBase.addTo(rawMap);
                }
            }
        });

        api.map.addControl('elevation-toggle', control, 'topleft');

        // Auto-enable if configured
        if (api.info.config.default_enabled) {
            var rawMap = api.map.getLeafletMap();
            rawMap.eachLayer(function(layer) {
                if (layer instanceof L.TileLayer) {
                    self._originalBase = layer;
                    rawMap.removeLayer(layer);
                }
            });
            self.tileLayer.addTo(rawMap);
        }

        // Listen for config changes via storage events
        this._onStorage = function(e) {
            if (e.key === storageKey && e.newValue && e.newValue !== self._currentProvider) {
                self._switchProvider(e.newValue);
            }
        };
        window.addEventListener('storage', this._onStorage);
    };

    ElevationMap.prototype._switchProvider = function(newKey) {
        var provider = providers[newKey] || providers.opentopomap;
        var rawMap = this.api.map.getLeafletMap();
        var wasActive = rawMap.hasLayer(this.tileLayer);

        if (wasActive) {
            rawMap.removeLayer(this.tileLayer);
        }

        this.tileLayer = L.tileLayer(provider.url, {
            maxZoom: provider.maxZoom,
            attribution: provider.attribution
        });
        this._currentProvider = newKey;

        if (wasActive) {
            this.tileLayer.addTo(rawMap);
        }
    };

    ElevationMap.prototype.onDisable = function(api) {
        // Restore original base if elevation is active
        var rawMap = api.map.getLeafletMap();
        if (rawMap.hasLayer(this.tileLayer)) {
            rawMap.removeLayer(this.tileLayer);
            if (this._originalBase) {
                this._originalBase.addTo(rawMap);
            }
        }
        api.map.removeControl('elevation-toggle');

        if (this._onStorage) {
            window.removeEventListener('storage', this._onStorage);
        }

        this.tileLayer = null;
        this._originalBase = null;
        this.api = null;
    };

    return ElevationMap;
})();

window.MeshPlugin = ElevationMapPlugin;
