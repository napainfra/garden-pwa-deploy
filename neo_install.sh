#!/bin/sh
# Append/replace Neo blinds cover config in HA configuration.yaml safely
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

# If a previous attempt added Neo entries, strip them out before re-adding
# Strip lines from the first 'platform: neosmartblinds' block to the next blank line / next top-level key
if grep -q "neosmartblinds" "$CFG"; then
  echo "Existing neosmartblinds entries detected, removing old version..."
  # Use python for surgical removal: remove every '- platform: neosmartblinds' block (until next '-' at same indent or top-level key)
  python3 << 'PYEOF'
import re
with open("/config/configuration.yaml") as f:
    txt = f.read()

# Match: '  - platform: neosmartblinds' through the end of its block.
# A block ends at the next '  - ' (next list item) or a blank-then-non-indented line or end of file.
lines = txt.splitlines(keepends=True)
out = []
skip = False
for i, ln in enumerate(lines):
    stripped = ln.strip()
    if not skip and stripped.startswith("- platform: neosmartblinds"):
        skip = True
        continue
    if skip:
        # End block when we hit a line that is NOT indented as a continuation
        # Continuation = starts with whitespace and not a sibling list item
        if ln.startswith("  - ") or (ln and not ln.startswith(" ") and not ln.startswith("\t")):
            # next sibling or top-level key reached
            skip = False
            out.append(ln)
        elif ln.strip() == "":
            # blank line ends the block
            skip = False
            out.append(ln)
        # else: still inside the block, drop the line
        continue
    out.append(ln)
with open("/config/configuration.yaml","w") as f:
    f.writelines(out)
PYEOF
fi

# Now check if there's already a top-level 'cover:' key. If yes, append entries (drop the 'cover:' header from neo file).
if grep -E "^cover:" "$CFG" >/dev/null 2>&1; then
  echo "Existing 'cover:' key found - appending entries only"
  echo "" >> "$CFG"
  tail -n +2 "$NEO" >> "$CFG"
else
  echo "No existing 'cover:' key - appending whole block"
  echo "" >> "$CFG"
  cat "$NEO" >> "$CFG"
fi

echo ""
echo "Done. Now in HA UI:"
echo "  1. Developer Tools -> YAML -> CHECK CONFIGURATION (must be green)"
echo "  2. If green: RELOAD COVER (or restart)"
echo "  3. New entities: cover.office_window, cover.guest_bedroom_blackout, cover.guest_bedroom_window"
echo ""
echo "If something breaks: cp $BACKUP $CFG && restart HA"
