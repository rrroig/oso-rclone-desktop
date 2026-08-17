"""Tray icon abstraction.

Three backends, picked to match the desktop:

* ``XApp.StatusIcon``  — native on Cinnamon (Linux Mint), MATE, Xfce, Budgie.
  Gives a real tooltip and separate left/right-click menus.
* ``AyatanaAppIndicator3`` / ``AppIndicator3`` — the StatusNotifierItem route,
  required on GNOME (with the AppIndicator extension) and KDE.
* ``Gtk.StatusIcon`` — legacy XEmbed tray, last resort.

Override the automatic choice with ``OSO_TRAY_BACKEND=xapp|appindicator|gtk``.
"""

import os

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk

from . import APP_ID, APP_NAME, util

XAPP = None
APPIND = None

try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp as _XApp

    XAPP = _XApp
except (ValueError, ImportError):
    XAPP = None

for _mod, _ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(_mod, _ver)
        APPIND = getattr(__import__("gi.repository", fromlist=[_mod]), _mod)
        break
    except (ValueError, ImportError, AttributeError):
        APPIND = None

ICON_IDLE = "oso-rclone-desktop-idle"
ICON_SYNCING = "oso-rclone-desktop-syncing"
ICON_ERROR = "oso-rclone-desktop-error"
ICON_PAUSED = "oso-rclone-desktop-paused"
ICON_WARNING = "oso-rclone-desktop-warning"

XAPP_DESKTOPS = ("cinnamon", "mate", "xfce", "budgie", "lxde", "lxqt")


def _prefer_xapp():
    desktop = util.desktop_name()
    return any(name in desktop for name in XAPP_DESKTOPS)


def pick_backend():
    forced = (os.environ.get("OSO_TRAY_BACKEND") or "").strip().lower()
    if forced in ("xapp", "appindicator", "gtk"):
        return forced
    order = ["xapp", "appindicator"] if _prefer_xapp() else ["appindicator", "xapp"]
    for name in order:
        if name == "xapp" and XAPP is not None:
            return "xapp"
        if name == "appindicator" and APPIND is not None:
            return "appindicator"
    return "gtk"


class TrayIcon:
    """Uniform interface over the three backends."""

    def __init__(self, menu, on_activate=None):
        self.menu = menu
        self.on_activate = on_activate
        self.backend = pick_backend()
        self._icon_name = ICON_IDLE
        self._impl = None
        self._build()

    # -------------------------------------------------- construction

    def _build(self):
        if self.backend == "xapp":
            icon = XAPP.StatusIcon()
            icon.set_name(APP_ID)
            icon.set_icon_name(self._icon_name)
            icon.set_tooltip_text(APP_NAME)
            icon.set_primary_menu(self.menu)
            icon.set_secondary_menu(self.menu)
            if self.on_activate:
                icon.connect("activate", lambda *_a: self.on_activate())
            self._impl = icon
        elif self.backend == "appindicator":
            indicator = APPIND.Indicator.new(
                APP_ID,
                self._icon_name,
                APPIND.IndicatorCategory.APPLICATION_STATUS,
            )
            if os.path.isdir(util.ICON_DIR):
                indicator.set_icon_theme_path(util.ICON_DIR)
            indicator.set_status(APPIND.IndicatorStatus.ACTIVE)
            indicator.set_title(APP_NAME)
            indicator.set_menu(self.menu)
            self._impl = indicator
        else:
            icon = Gtk.StatusIcon()
            icon.set_from_icon_name(self._icon_name)
            icon.set_tooltip_text(APP_NAME)
            icon.set_title(APP_NAME)
            icon.connect("popup-menu", self._on_gtk_popup)
            if self.on_activate:
                icon.connect("activate", lambda *_a: self.on_activate())
            self._impl = icon

    def _on_gtk_popup(self, icon, button, activate_time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    # -------------------------------------------------- updates

    def set_icon(self, name):
        if name == self._icon_name:
            return
        self._icon_name = name
        if self.backend == "xapp":
            self._impl.set_icon_name(name)
        elif self.backend == "appindicator":
            self._impl.set_icon_full(name, APP_NAME)
        else:
            self._impl.set_from_icon_name(name)

    def set_tooltip(self, text):
        if self.backend == "xapp":
            self._impl.set_tooltip_text(text)
        elif self.backend == "appindicator":
            # AppIndicator has no tooltip; the first (insensitive) menu row
            # carries the status text instead.
            self._impl.set_title(text)
        else:
            self._impl.set_tooltip_text(text)

    def set_label(self, text):
        """Short text next to the icon (AppIndicator/XApp only)."""
        if self.backend == "appindicator":
            self._impl.set_label(text or "", APP_NAME)
        elif self.backend == "xapp":
            self._impl.set_label(text or "")

    def set_attention(self, attention):
        if self.backend == "appindicator":
            status = (
                APPIND.IndicatorStatus.ATTENTION
                if attention
                else APPIND.IndicatorStatus.ACTIVE
            )
            self._impl.set_status(status)

    def refresh_menu(self):
        if self.backend == "appindicator":
            self._impl.set_menu(self.menu)
        elif self.backend == "xapp":
            self._impl.set_primary_menu(self.menu)
            self._impl.set_secondary_menu(self.menu)

    def describe(self):
        return {
            "xapp": "XApp.StatusIcon (Cinnamon/MATE/Xfce native)",
            "appindicator": "AppIndicator / StatusNotifierItem",
            "gtk": "Gtk.StatusIcon (legacy tray)",
        }[self.backend]
