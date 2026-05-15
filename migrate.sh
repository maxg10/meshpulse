#!/bin/bash
# MeshPulse migration script: /meshtastic/ → /meshpulse/
# Run once after upgrading from Meshtastic Network Mapper to MeshPulse
# NOTE: Run this ONCE when upgrading from Meshtastic Network Mapper to MeshPulse.
# After migration, future updates only need: git pull && ./install.sh

set -e
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "🔄 MeshPulse Migration: /meshtastic/ → /meshpulse/"
echo ""

# Copy data files from old location to new
if [ -d "/var/www/html/meshtastic" ]; then
    echo "📦 Copying data from /var/www/html/meshtastic/ to /var/www/html/meshpulse/..."
    sudo mkdir -p /var/www/html/meshpulse
    # Copy data files only (not frontend HTML - those come from install.sh)
    for f in nodes.json stats.db config.json; do
        if [ -f "/var/www/html/meshtastic/$f" ]; then
            sudo cp "/var/www/html/meshtastic/$f" "/var/www/html/meshpulse/$f"
            echo -e "  ${GREEN}✅ $f copied${NC}"
        fi
    done
    sudo chown -R $(whoami):$(whoami) /var/www/html/meshpulse
    echo ""
    echo -e "${GREEN}✅ Data migrated successfully${NC}"
else
    echo -e "${YELLOW}⚠️  /var/www/html/meshtastic/ not found - nothing to migrate${NC}"
fi

echo ""
echo "🌐 Setting up redirect /meshtastic/ → /meshpulse/ in lighttpd..."
sudo mkdir -p /etc/lighttpd/conf-available
cat << 'EOF' | sudo tee /etc/lighttpd/conf-available/meshpulse.conf > /dev/null
# MeshPulse - redirect legacy /meshtastic/ to /meshpulse/
$HTTP["url"] =~ "^/meshtastic(/.*)?$" {
    url.redirect = ( "^/meshtastic(/.*)?$" => "/meshpulse$1" )
}
EOF
sudo ln -sf /etc/lighttpd/conf-available/meshpulse.conf /etc/lighttpd/conf-enabled/meshpulse.conf
sudo systemctl reload lighttpd 2>/dev/null || sudo service lighttpd reload 2>/dev/null || true
echo -e "${GREEN}✅ Redirect configured${NC}"

echo ""
echo -e "${GREEN}🎉 Migration complete!${NC}"
echo "   Old URL still works: http://YOUR_PI_IP/meshtastic/ → redirects to /meshpulse/"
echo "   New URL: http://YOUR_PI_IP/meshpulse/"
