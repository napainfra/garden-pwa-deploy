#!/bin/sh
set -e
echo "removing old folder..."
rm -rf /addons/garden_pwa
cd /addons
echo "downloading zip..."
wget -q https://raw.githubusercontent.com/napainfra/garden-pwa-deploy/main/garden-pwa-addon.zip
echo "unzipping..."
unzip -oq garden-pwa-addon.zip
rm garden-pwa-addon.zip
echo ""
echo "==== files ===="
ls /addons/garden_pwa
echo ""
echo "==== version ===="
head -3 /addons/garden_pwa/config.yaml
echo ""
echo "DONE"
