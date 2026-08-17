#!/usr/bin/env bash
# Build a .deb package for OSO Rclone Desktop.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"
APP_ID="oso-rclone-desktop"
PKG="oso_rclone_desktop"
VERSION="$(python3 -c "import re;print(re.search(r'__version__ = \"([^\"]+)\"', open('$PKG/__init__.py').read()).group(1))")"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

install -d "$BUILD/DEBIAN" \
  "$BUILD/usr/lib/$APP_ID/$PKG/icons" \
  "$BUILD/usr/bin" \
  "$BUILD/usr/share/applications" \
  "$BUILD/usr/share/icons/hicolor/scalable/apps" \
  "$BUILD/usr/share/doc/$APP_ID"

install -m 0644 "$PKG"/*.py           "$BUILD/usr/lib/$APP_ID/$PKG/"
install -m 0644 "$PKG/icons"/*.svg    "$BUILD/usr/lib/$APP_ID/$PKG/icons/"
install -m 0644 "$PKG/icons"/*.svg    "$BUILD/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "data/$APP_ID.desktop" "$BUILD/usr/share/applications/"
install -m 0644 README.md LICENSE     "$BUILD/usr/share/doc/$APP_ID/"

cat > "$BUILD/usr/bin/$APP_ID" <<'LAUNCH'
#!/usr/bin/env python3
"""Launcher for OSO Rclone Desktop."""
import sys

sys.path.insert(0, "/usr/lib/oso-rclone-desktop")

from oso_rclone_desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
LAUNCH
chmod 0755 "$BUILD/usr/bin/$APP_ID"

cat > "$BUILD/DEBIAN/control" <<CONTROL
Package: $APP_ID
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Maintainer: Jose Roig Borrell <rrroig@gmail.com>
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, gir1.2-notify-0.7, xdg-utils
Recommends: rclone (>= 1.66), gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1 | gir1.2-xapp-1.0, fuse3
Suggests: gnome-shell-extension-appindicator
Homepage: https://github.com/rrroig/oso-rclone-desktop
Description: Dropbox-style tray app to sync folders with Google Drive via rclone
 Keeps a local folder and a Google Drive folder in sync in the background using
 rclone bisync, with a tray status icon, desktop notifications, conflict review
 and a settings window for accounts, folders and logs.
 .
 Works on GNOME (with the AppIndicator extension) and natively on Cinnamon,
 MATE and Xfce through XApp.
CONTROL

cat > "$BUILD/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
POSTINST
chmod 0755 "$BUILD/DEBIAN/postinst"

cat > "$BUILD/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
case "$1" in
  remove|purge)
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
      gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
      update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
    fi
    ;;
esac
exit 0
POSTRM
chmod 0755 "$BUILD/DEBIAN/postrm"

mkdir -p dist
OUT="dist/${APP_ID}_${VERSION}_all.deb"
dpkg-deb --build --root-owner-group "$BUILD" "$OUT" >/dev/null
echo "built $OUT"
dpkg-deb --info "$OUT" | sed -n '1,12p'
