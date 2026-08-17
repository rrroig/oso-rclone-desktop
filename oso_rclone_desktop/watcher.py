"""Recursive local folder watcher built on GIO file monitors.

Used to trigger a sync shortly after the user touches a file, the way
Dropbox does, instead of waiting for the next scheduled run.
"""

import os

from gi.repository import Gio, GLib

MAX_WATCHES = 4000
SKIP_DIRS = {".git", ".cache", "node_modules", "__pycache__", ".Trash-1000"}


class RecursiveWatcher:
    """Watch a directory tree and call ``on_change`` (debounced) on activity."""

    def __init__(self, root, on_change, debounce_seconds=15):
        self.root = root
        self.on_change = on_change
        self.debounce_seconds = max(2, int(debounce_seconds))
        self._monitors = {}
        self._timer = None
        self._running = False
        self.truncated = False

    # -------------------------------------------------- lifecycle

    def start(self):
        if self._running:
            return
        self._running = True
        self.truncated = False
        self._add_tree(self.root)

    def stop(self):
        self._running = False
        self._cancel_timer()
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()

    @property
    def watch_count(self):
        return len(self._monitors)

    # -------------------------------------------------- internals

    def _add_tree(self, path):
        if not os.path.isdir(path):
            return
        self._add_dir(path)
        for dirpath, dirnames, _files in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in dirnames:
                if len(self._monitors) >= MAX_WATCHES:
                    self.truncated = True
                    return
                self._add_dir(os.path.join(dirpath, name))

    def _add_dir(self, path):
        if path in self._monitors or len(self._monitors) >= MAX_WATCHES:
            if path not in self._monitors:
                self.truncated = True
            return
        try:
            gfile = Gio.File.new_for_path(path)
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        if monitor is None:
            return
        monitor.set_rate_limit(1000)
        monitor.connect("changed", self._on_monitor_event)
        self._monitors[path] = monitor

    def _on_monitor_event(self, _monitor, gfile, _other, event):
        if not self._running:
            return
        path = gfile.get_path() or ""
        base = os.path.basename(path)
        if base.startswith(".goutputstream-") or base.endswith("~"):
            return
        if event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN):
            if os.path.isdir(path) and base not in SKIP_DIRS:
                self._add_tree(path)
        elif event == Gio.FileMonitorEvent.DELETED:
            monitor = self._monitors.pop(path, None)
            if monitor:
                monitor.cancel()
        elif event == Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            pass
        elif event not in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        ):
            return
        self._schedule()

    def _schedule(self):
        self._cancel_timer()
        self._timer = GLib.timeout_add_seconds(self.debounce_seconds, self._fire)

    def _cancel_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None

    def _fire(self):
        self._timer = None
        if self._running:
            self.on_change()
        return GLib.SOURCE_REMOVE
