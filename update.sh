#!/bin/bash
#
# MeshPulse - Updater
# https://github.com/maxg10/meshpulse
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "========================================"
echo " MeshPulse - Updater"
echo "========================================"
echo ""

# Check if running from correct directory
if [ ! -f "backend/meshpulse.py" ]; then
    echo "❌ Error: Run this script from the repository root directory"
    echo "   cd ~/meshpulse && ./update.sh"
    exit 1
fi

echo "📥 Pulling latest changes from GitHub..."
git pull origin main

echo ""
echo "🔧 Running installer..."
./install.sh

echo ""
echo "🔄 Restarting service..."
sudo systemctl restart meshpulse

echo ""
echo "========================================"
echo -e "${GREEN}✅ Update complete!${NC}"
echo "========================================"
echo ""
echo "Version installed:"
grep "MAPPER_VERSION" /var/www/html/meshtastic/index.html | grep -o "'[0-9.]*'" | head -1
echo ""
sudo systemctl status meshpulse --no-pager -l | head -5
