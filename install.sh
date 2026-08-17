#!/usr/bin/env bash
# Installer for OSO Rclone Desktop (Ubuntu / Linux Mint / Debian-based).
#
#   ./install.sh              install for the current user (~/.local, no root)
#   ./install.sh --system     install for every user (/usr/local, needs sudo)
#   ./install.sh --no-deps    skip apt/rclone installation
#   ./install.sh --autostart  also enable start-at-login
#   ./install.sh --no-context-menu   skip the file-manager right-click entries
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="oso-rclone-desktop"
PKG="oso_rclone_desktop"

MODE="user"
DO_DEPS=1
DO_CONTEXT=1
DO_AUTOSTART=0
for arg in "$@"; do
  case "$arg" in
    --system) MODE="system" ;;
    --user) MODE="user" ;;
    --no-deps) DO_DEPS=0 ;;
    --no-context-menu) DO_CONTEXT=0 ;;
    --autostart) DO_AUTOSTART=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "system" ]; then
  PREFIX="/usr/local"
  LIBDIR="$PREFIX/lib/$APP_ID"
  BINDIR="$PREFIX/bin"
  DATADIR="$PREFIX/share"
  SUDO="sudo"
else
  PREFIX="$HOME/.local"
  LIBDIR="$PREFIX/lib/$APP_ID"
  BINDIR="$PREFIX/bin"
  DATADIR="$PREFIX/share"
  SUDO=""
fi

info()  { printf '\033[1;34m::\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✔\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m ⚠\033[0m %s\n' "$*"; }

DESKTOP="${XDG_CURRENT_DESKTOP:-${DESKTOP_SESSION:-unknown}}"
info "Desktop: $DESKTOP · install mode: $MODE · prefix: $PREFIX"

# --------------------------------------------------------------- dependencies

APT_PKGS=(python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-notify-0.7 xdg-utils)

# Tray backend: XApp is native on Cinnamon/MATE/Xfce, AppIndicator on GNOME/KDE.
case "${DESKTOP,,}" in
  *cinnamon*|*mate*|*xfce*|*budgie*) APT_PKGS+=(gir1.2-xapp-1.0) ;;
esac
if apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
  APT_PKGS+=(gir1.2-ayatanaappindicator3-0.1)
else
  APT_PKGS+=(gir1.2-appindicator3-0.1)
fi
# fuse3 is only needed for "mount" mode, but it is tiny.
APT_PKGS+=(fuse3)

missing=()
if [ "$DO_DEPS" = "1" ] && command -v dpkg-query >/dev/null 2>&1; then
  for pkg in "${APT_PKGS[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
      missing+=("$pkg")
    fi
  done
fi

if [ ${#missing[@]} -gt 0 ]; then
  info "Installing packages: ${missing[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${missing[@]}"
  ok "Dependencies installed"
elif [ "$DO_DEPS" = "1" ]; then
  ok "All system dependencies already present"
fi

# --------------------------------------------------------------- rclone

rclone_version_ok() {
  command -v rclone >/dev/null 2>&1 || return 1
  local v major minor
  v="$(rclone version 2>/dev/null | head -1 | sed -E 's/^rclone v?([0-9]+)\.([0-9]+).*/\1 \2/')"
  major="${v%% *}"; minor="${v##* }"
  [ -n "$major" ] || return 1
  [ "$major" -gt 1 ] && return 0
  [ "$major" -eq 1 ] && [ "$minor" -ge 66 ]
}

if [ "$DO_DEPS" = "1" ]; then
  if rclone_version_ok; then
    ok "rclone $(rclone version | head -1 | awk '{print $2}') is recent enough"
  else
    if command -v rclone >/dev/null 2>&1; then
      warn "rclone $(rclone version | head -1 | awk '{print $2}') is too old for two-way sync (needs v1.66+)."
    else
      warn "rclone is not installed."
    fi
    echo "    The official installer fetches the current release from rclone.org."
    read -r -p "    Install/upgrade rclone now with sudo? [Y/n] " answer
    if [[ ! "${answer:-Y}" =~ ^[Nn] ]]; then
      curl -fsSL https://rclone.org/install.sh | sudo bash
      ok "rclone $(rclone version | head -1 | awk '{print $2}') installed"
    else
      warn "Skipped. Two-way sync needs rclone 1.66 or newer."
    fi
  fi
fi

# --------------------------------------------------------------- GNOME extension

if [[ "${DESKTOP,,}" == *gnome* && "${DESKTOP,,}" != *cinnamon* ]]; then
  ext_dir_sys="/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com"
  ext_dir_alt="/usr/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com"
  if [ -d "$ext_dir_sys" ] || [ -d "$ext_dir_alt" ] || [ -d "$HOME/.local/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com" ]; then
    ok "GNOME AppIndicator support is present"
  else
    warn "GNOME needs the AppIndicator extension to show tray icons."
    read -r -p "    Install gnome-shell-extension-appindicator now? [Y/n] " answer
    if [[ ! "${answer:-Y}" =~ ^[Nn] ]]; then
      sudo apt-get install -y gnome-shell-extension-appindicator || true
      warn "Log out and back in (or press Alt+F2, r on X11) to load the extension."
    fi
  fi
  if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable ubuntu-appindicators@ubuntu.com 2>/dev/null || true
    gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com 2>/dev/null || true
  fi
fi

# --------------------------------------------------------------- files

info "Installing application files"
$SUDO rm -rf "$LIBDIR/$PKG"
$SUDO install -d "$LIBDIR/$PKG" "$LIBDIR/$PKG/icons" "$BINDIR" \
  "$DATADIR/applications" "$DATADIR/icons/hicolor/scalable/apps"

$SUDO install -m 0644 "$SRC_DIR/$PKG"/*.py "$LIBDIR/$PKG/"
$SUDO install -m 0644 "$SRC_DIR/$PKG/icons"/*.svg "$LIBDIR/$PKG/icons/"
$SUDO install -m 0644 "$SRC_DIR/$PKG/icons"/*.svg "$DATADIR/icons/hicolor/scalable/apps/"
$SUDO install -m 0644 "$SRC_DIR/data/$APP_ID.desktop" "$DATADIR/applications/"

launcher="$(mktemp)"
cat > "$launcher" <<EOF
#!/usr/bin/env python3
"""Launcher for OSO Rclone Desktop."""
import sys

sys.path.insert(0, "$LIBDIR")

from oso_rclone_desktop.app import main

if __name__ == "__main__":
    sys.exit(main())
EOF
$SUDO install -m 0755 "$launcher" "$BINDIR/$APP_ID"
rm -f "$launcher"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  $SUDO gtk-update-icon-cache -f -t "$DATADIR/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  $SUDO update-desktop-database "$DATADIR/applications" >/dev/null 2>&1 || true
fi
ok "Installed to $LIBDIR"

if [ "$MODE" = "user" ] && ! echo ":$PATH:" | grep -q ":$BINDIR:"; then
  warn "$BINDIR is not in your PATH. Add this to ~/.profile:"
  echo '        export PATH="$HOME/.local/bin:$PATH"'
fi

# --------------------------------------------------------------- file managers

# Right-click entries are per-user by nature, so they go under $HOME even for a
# system-wide install. Each file manager wants a different mechanism.
if [ "$DO_CONTEXT" = "1" ]; then
  installed_for=""

  # Nemo (Cinnamon / Linux Mint) — native action file, appears at top level
  if command -v nemo >/dev/null 2>&1; then
    mkdir -p "$HOME/.local/share/nemo/actions"
    install -m 0644 "$SRC_DIR/contextmenu/$APP_ID.nemo_action" \
      "$HOME/.local/share/nemo/actions/"
    installed_for="$installed_for Nemo"
  fi

  # Caja (MATE) — scripts folder
  if command -v caja >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/caja/scripts"
    install -m 0755 "$SRC_DIR/contextmenu/Sync with Google Drive" \
      "$HOME/.config/caja/scripts/"
    installed_for="$installed_for Caja"
  fi

  # Nautilus (GNOME) — a real menu entry when python3-nautilus is present,
  # otherwise a script under the Scripts submenu (no dependencies).
  if command -v nautilus >/dev/null 2>&1; then
    if python3 -c "import gi; gi.require_version('Nautilus', '4.0')" 2>/dev/null ||
       python3 -c "import gi; gi.require_version('Nautilus', '3.0')" 2>/dev/null; then
      mkdir -p "$HOME/.local/share/nautilus-python/extensions"
      install -m 0644 "$SRC_DIR/contextmenu/nautilus_extension.py" \
        "$HOME/.local/share/nautilus-python/extensions/oso_rclone_desktop.py"
      installed_for="$installed_for Nautilus"
    else
      mkdir -p "$HOME/.local/share/nautilus/scripts"
      install -m 0755 "$SRC_DIR/contextmenu/Sync with Google Drive" \
        "$HOME/.local/share/nautilus/scripts/"
      installed_for="$installed_for Nautilus(script)"
      warn "For a top-level Nautilus entry instead of Scripts →, install python3-nautilus."
    fi
  fi

  # Dolphin (KDE) — service menu, both the current and the legacy location
  if command -v dolphin >/dev/null 2>&1; then
    for dir in "$HOME/.local/share/kio/servicemenus" \
               "$HOME/.local/share/kservices5/ServiceMenus"; do
      mkdir -p "$dir"
      install -m 0755 "$SRC_DIR/contextmenu/$APP_ID-servicemenu.desktop" "$dir/"
    done
    installed_for="$installed_for Dolphin"
  fi

  if [ -n "$installed_for" ]; then
    ok "Right-click entry added for:$installed_for"
    if command -v nautilus >/dev/null 2>&1; then
      echo "   (restart the file manager to see it: nautilus -q / nemo -q)"
    fi
  else
    warn "No supported file manager found — skipped the right-click entry."
  fi
  if command -v thunar >/dev/null 2>&1; then
    echo "   Thunar: add a custom action manually with"
    echo "     Edit → Configure custom actions → +, command: $APP_ID --sync-path %f"
  fi
fi

# --------------------------------------------------------------- autostart

if [ "$DO_AUTOSTART" = "1" ]; then
  mkdir -p "$HOME/.config/autostart"
  sed "s|^Exec=.*|Exec=$BINDIR/$APP_ID|" "$SRC_DIR/data/$APP_ID.desktop" \
    > "$HOME/.config/autostart/$APP_ID.desktop"
  printf 'X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=8\n' \
    >> "$HOME/.config/autostart/$APP_ID.desktop"
  ok "Start-at-login enabled"
fi

echo
ok "Done. Launch it from your applications menu, or run: $APP_ID"
echo "   First run opens the settings window: connect Google Drive, then add a folder."
