#!/usr/bin/env bash
# Remove OSO Rclone Desktop. Config, logs and your synced files are kept
# unless you pass --purge.
set -euo pipefail
APP_ID="oso-rclone-desktop"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

for prefix in "$HOME/.local" "/usr/local"; do
  SUDO=""; [ "$prefix" = "/usr/local" ] && SUDO="sudo"
  for path in \
    "$prefix/lib/$APP_ID" \
    "$prefix/bin/$APP_ID" \
    "$prefix/share/applications/$APP_ID.desktop"; do
    if [ -e "$path" ]; then
      $SUDO rm -rf "$path"
      echo "removed $path"
    fi
  done
  $SUDO rm -f "$prefix"/share/icons/hicolor/scalable/apps/$APP_ID*.svg 2>/dev/null || true
done

rm -f "$HOME/.config/autostart/$APP_ID.desktop"

# file-manager right-click entries
rm -f "$HOME/.local/share/nemo/actions/$APP_ID.nemo_action" \
      "$HOME/.local/share/nautilus-python/extensions/oso_rclone_desktop.py" \
      "$HOME/.local/share/nautilus/scripts/Sync with Google Drive" \
      "$HOME/.config/caja/scripts/Sync with Google Drive" \
      "$HOME/.local/share/kio/servicemenus/$APP_ID-servicemenu.desktop" \
      "$HOME/.local/share/kservices5/ServiceMenus/$APP_ID-servicemenu.desktop" 2>/dev/null || true
pkill -f "oso_rclone_desktop" 2>/dev/null || true

if [ "$PURGE" = "1" ]; then
  rm -rf "$HOME/.config/$APP_ID" "$HOME/.local/state/$APP_ID" "$HOME/.cache/$APP_ID"
  echo "purged configuration and logs"
else
  echo "kept configuration in ~/.config/$APP_ID (use --purge to delete)"
fi
echo "Your rclone remotes (~/.config/rclone) and synced files were not touched."
