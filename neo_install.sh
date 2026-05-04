#!/bin/sh
# Append Neo blinds cover config to HA configuration.yaml safely
set -e

CFG=/config/configuration.yaml
NEO=/tmp/neo_blinds.yaml
BACKUP=/config/configuration.yaml.bak.$(date +%s)

if [ ! -f "$NEO" ]; then
  echo "ERROR: $NEO not found"
  exit 1
fi

cp "$CFG" "$BACKUP"
echo "Backed up $CFG -> $BACKUP"

# Check if already installed
if grep -q "078.001-00" "$CFG"; then
  echo "Neo blinds already in $CFG, skipping append"
  exit 0
fi

# Check if there's already a top-level 'cover:' key. If yes, strip the 'cover:' line from neo_blinds.yaml.
if grep -E "^cover:" "$CFG" >/dev/null 2>&1; then
  echo "Existing 'cover:' key found - appending entries only"
  # Skip first line "cover:" from NEO file
  tail -n +2 "$NEO" >> "$CFG"
else
  echo "No existing 'cover:' key - appending whole block"
  echo "" >> "$CFG"
  cat "$NEO" >> "$CFG"
fi

echo ""
echo "Done. Now:"
echo "  1. HA UI -> Developer Tools -> YAML -> CHECK CONFIGURATION"
echo "  2. If green: Reload Cover (or restart HA)"
echo "  3. New entities: cover.office_window, cover.guest_bedroom_blackout, cover.guest_bedroom_window"
echo ""
echo "If something breaks: cp $BACKUP $CFG && restart HA"
