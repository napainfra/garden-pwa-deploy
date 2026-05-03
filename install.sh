#!/bin/sh
set -e
echo "[1/5] Removing old folder..."
rm -rf /addons/garden_pwa
echo "[2/5] Downloading v1.0.1..."
cd /addons
wget -q https://raw.githubusercontent.com/napainfra/garden-pwa-deploy/main/garden-pwa-addon.zip
echo "[3/5] Unzipping..."
unzip -oq garden-pwa-addon.zip
rm garden-pwa-addon.zip
echo "[4/5] Files in /addons/garden_pwa:"
ls /addons/garden_pwa
echo "[5/5] Version:"
head -3 /addons/garden_pwa/config.yaml
echo ""
echo "DONE. Now in HA UI: Settings -> Apps -> top-right 3-dot menu -> Check for updates"
