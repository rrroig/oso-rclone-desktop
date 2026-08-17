"""Top-level Nautilus right-click entry for OSO Rclone Desktop.

Installed to ~/.local/share/nautilus-python/extensions/ when the
python3-nautilus bindings are available. Without them, install.sh falls back to
a plain script under Nautilus' "Scripts" submenu, which needs no dependencies.
"""

import os
import subprocess
from urllib.parse import unquote, urlparse

import gi

# Nautilus 43+ ships the 4.0 typelib, GNOME 42 and older ship 3.0.
for _version in ("4.0", "3.0"):
    try:
        gi.require_version("Nautilus", _version)
        break
    except ValueError:
        continue

from gi.repository import GObject, Nautilus  # noqa: E402

COMMAND = "oso-rclone-desktop"


def _path(item):
    return unquote(urlparse(item.get_uri()).path)


class OsoRcloneMenu(GObject.GObject, Nautilus.MenuProvider):
    def _item(self, path):
        item = Nautilus.MenuItem(
            name="OsoRclone::sync",
            label="Sync with Google Drive",
            tip="Sync this folder with OSO Rclone Desktop",
            icon="oso-rclone-desktop",
        )
        item.connect("activate", lambda *_a: self._run(path))
        return item

    @staticmethod
    def _run(path):
        try:
            subprocess.Popen(
                [COMMAND, "--sync-path", path],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def get_file_items(self, *args):
        files = args[-1]
        if len(files) != 1 or not files[0].is_directory():
            return []
        return [self._item(_path(files[0]))]

    def get_background_items(self, *args):
        folder = args[-1]
        path = _path(folder)
        return [self._item(path)] if os.path.isdir(path) else []
