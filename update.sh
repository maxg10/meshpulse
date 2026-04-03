#!/bin/bash
#
# Meshtastic Network Mapper - Updater
# https://github.com/maxg10/meshtastic-network-mapper
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "========================================"
echo " Meshtastic Network Mapper - Updater"
echo "========================================"
echo ""

# Check if running from correct directory
if [ ! -f "backend/meshtastic_mapper.py" ]; then
    echo "❌ Error: Run this script from the repository root directory"
    echo "   cd ~/meshtastic-network-mapper && ./update.sh"
    exit 1
fi

echo "📥 Pulling latest changes from GitHub..."
git pull origin main

echo ""
echo "🔧 Running installer..."
./install.sh

echo ""
echo "🔄 Restarting service..."
sudo systemctl restart meshtastic-mapper

echo ""
echo "========================================"
echo -e "${GREEN}✅ Update complete!${NC}"
echo "========================================"
echo ""
echo "Version installed:"
grep "MAPPER_VERSION" /var/www/html/meshtastic/index.html | grep -o "'[0-9.]*'" | head -1
echo ""
sudo systemctl status meshtastic-mapper --no-pager -l | head -5
