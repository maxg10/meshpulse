#!/bin/bash
# Build .meshplugin packages for distribution
set -e

mkdir -p dist

# BBS
echo "📦 Building bbs..."
cd plugins/maxg10/bbs
zip -r ../../../dist/bbs-1.0.4.meshplugin plugin.json frontend/ backend/
cd ../../..

# Weather Overlay
echo "📦 Building weather-overlay..."
cd plugins/maxg10/weather-overlay
zip -r ../../../dist/weather-overlay-1.0.0.meshplugin plugin.json frontend/
cd ../../..

echo ""
echo "✅ Packages built in dist/"
ls -la dist/*.meshplugin
