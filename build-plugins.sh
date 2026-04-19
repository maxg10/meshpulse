#!/bin/bash
# Build .meshplugin packages for distribution
set -e

mkdir -p dist

# Elevation Map
echo "📦 Building elevation-map..."
cd plugins/maxg10/elevation-map
zip -r ../../../dist/elevation-map-1.0.1.meshplugin plugin.json frontend/
cd ../../..

# MQTT Proxy
echo "📦 Building mqtt-proxy..."
cd plugins/maxg10/mqtt-proxy
zip -r ../../../dist/mqtt-proxy-1.0.0.meshplugin plugin.json requirements.txt backend/
cd ../../..

echo ""
echo "✅ Packages built in dist/"
ls -la dist/*.meshplugin
