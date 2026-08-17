"""Application entry point: tray menu, notifications, single-instance guard."""

import fcntl
import os
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
except (ValueError, ImportError):  # pragma: no cover - optional
    Notify = None

from gi.repository import GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, __version__, engine as eng, rclone, tray, util  # noqa: E402
from .config import Config, State  # noqa: E402
from .settings import ConflictsDialog, SettingsWindow, ask_config_password  # noqa: E402

STATE_ICON = {
    eng.IDLE: tray.ICON_IDLE,
    eng.SYNCING: tray.ICON_SYNCING,
    eng.ERROR: tray.ICON_ERROR,
    eng.NEEDS_RESYNC: tray.ICON_WARNING,
    eng.PAUSED: tray.ICON_PAUSED,
    eng.DISABLED: tray.ICON_PAUSED,
    eng.MOUNTED: tray.ICON_IDLE,
    eng.OFFLINE: tray.ICON_WARNING,
}


class Application:
    def __init__(self):
        util.ensure_dirs()
        self._lock_fd = None
        self.config = Config()
        self.state = State()
        self.settings_window = None
        self._anim_timer = None
        self._anim_flip = False

        icon_theme = Gtk.IconTheme.get_default()
        icon_theme.append_search_path(util.ICON_DIR)

        if Notify:
            Notify.init(APP_NAME)
        self._notification = None

        self.engine = eng.Engine(
            self.config,
            self.state,
            on_change=self._on_engine_change,
            on_notify=self.notify,
        )
        self.menu = Gtk.Menu()
        self.tray = tray.TrayIcon(self.menu, on_activate=self.open_settings)
        self.rebuild_menu()

    # -------------------------------------------------- single instance

    def acquire_lock(self):
        try:
            self._lock_fd = os.open(util.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self._lock_fd, 0)
            os.write(self._lock_fd, str(os.getpid()).encode())
            return True
        except OSError:
            return False

    # -------------------------------------------------- menu

    def rebuild_menu(self):
        for child in self.menu.get_children():
            self.menu.remove(child)

        self.header_item = Gtk.MenuItem(label=APP_NAME)
        self.header_item.set_sensitive(False)
        self.menu.append(self.header_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        if not rclone.is_installed():
            item = Gtk.MenuItem(label="rclone is not installed — open Settings")
            item.connect("activate", lambda *_a: self.open_settings(page=0))
            self.menu.append(item)
            self.menu.append(Gtk.SeparatorMenuItem())
        elif not self.config.jobs:
            item = Gtk.MenuItem(label="Set up a folder to sync…")
            item.connect("activate", lambda *_a: self.open_settings(page=1))
            self.menu.append(item)
            self.menu.append(Gtk.SeparatorMenuItem())

        self.job_items = {}
        single = len(self.config.jobs) == 1
        for job in self.config.jobs:
            runner = self.engine.runner(job["id"])
            item = Gtk.MenuItem(label=job.get("name", "Sync"))
            submenu = self._build_job_menu(job, runner)
            item.set_submenu(submenu)
            self.menu.append(item)
            self.job_items[job["id"]] = item
            if single:
                for entry in submenu.get_children():
                    submenu.remove(entry)
                    self.menu.append(entry)
                self.menu.remove(item)
                self.job_items[job["id"]] = None

        if self.config.jobs:
            self.menu.append(Gtk.SeparatorMenuItem())
            sync_all = Gtk.MenuItem(label="Sync all now")
            sync_all.connect("activate", lambda *_a: self.engine.sync_all())
            self.menu.append(sync_all)

            self.pause_item = Gtk.CheckMenuItem(label="Pause syncing")
            self.pause_item.set_active(self.engine.paused)
            self.pause_item.connect("toggled", self._on_pause_toggled)
            self.menu.append(self.pause_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        settings_item = Gtk.MenuItem(label="Settings…")
        settings_item.connect("activate", lambda *_a: self.open_settings())
        self.menu.append(settings_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_a: self.quit())
        self.menu.append(quit_item)

        self.menu.show_all()
        self._menu_built = True
        self.tray.refresh_menu()
        self.update_ui()

    def _build_job_menu(self, job, runner):
        submenu = Gtk.Menu()
        status = Gtk.MenuItem(label=runner.status_text() if runner else "…")
        status.set_sensitive(False)
        submenu.append(status)
        self._status_items = getattr(self, "_status_items", {})
        self._status_items[job["id"]] = status
        submenu.append(Gtk.SeparatorMenuItem())

        entries = [
            ("Sync now", lambda *_a: self._job_action(job["id"], "sync")),
            ("Open folder", lambda *_a: self._job_action(job["id"], "open")),
            ("Open on Google Drive", lambda *_a: self._job_action(job["id"], "web")),
            ("View log", lambda *_a: self._job_action(job["id"], "log")),
        ]
        if runner is not None and runner.conflicts:
            entries.insert(
                0,
                (
                    "Review %d conflict(s)…" % len(runner.conflicts),
                    lambda *_a: self._job_action(job["id"], "conflicts"),
                ),
            )
        if (job.get("mode") or "bisync") == "bisync":
            entries.insert(1, ("Run first sync…", lambda *_a: self._job_action(job["id"], "resync")))
        for label, handler in entries:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            submenu.append(item)
        submenu.show_all()
        return submenu

    def _job_action(self, job_id, action):
        runner = self.engine.runner(job_id)
        job = self.config.job(job_id)
        if not job:
            return
        if action == "sync" and runner:
            runner.request_sync("manual")
        elif action == "resync" and runner:
            self.open_settings(page=1)
            if self.settings_window:
                self.settings_window._current_job_id = job_id
                self.settings_window.refresh_jobs()
                self.settings_window._on_resync(None)
        elif action == "open":
            path = os.path.expanduser(job.get("local_path") or "")
            if path:
                os.makedirs(path, exist_ok=True)
                util.open_path(path)
        elif action == "conflicts" and runner:
            self.open_settings(page=1)
            ConflictsDialog(self.settings_window, runner)
        elif action == "web":
            util.open_path("https://drive.google.com/drive/my-drive")
        elif action == "log":
            self.open_settings(page=2)
            if self.settings_window:
                self.settings_window._select_log(job_id)

    def _on_pause_toggled(self, item):
        self.engine.set_paused(item.get_active())

    # -------------------------------------------------- ui updates

    def _on_engine_change(self, _runner=None):
        GLib.idle_add(self.update_ui)

    def update_ui(self):
        overall = self.engine.overall_state()
        signature = tuple(len(r.conflicts) for r in self.engine.runners)
        if signature != getattr(self, "_conflict_signature", None):
            self._conflict_signature = signature
            if getattr(self, "_menu_built", False):
                GLib.idle_add(self.rebuild_menu)
                return GLib.SOURCE_REMOVE
        self.tray.set_icon(STATE_ICON.get(overall, tray.ICON_IDLE))
        self.tray.set_attention(overall in (eng.ERROR, eng.NEEDS_RESYNC))

        lines = []
        for runner in self.engine.runners:
            lines.append("%s: %s" % (runner.name, runner.status_text()))
            item = getattr(self, "_status_items", {}).get(runner.id)
            if item:
                item.set_label(runner.status_text())
            menu_item = self.job_items.get(runner.id) if hasattr(self, "job_items") else None
            if menu_item:
                menu_item.set_label("%s — %s" % (runner.name, eng.STATE_LABELS.get(runner.state, "")))
        summary = "\n".join(lines) or "No folders configured"
        self.tray.set_tooltip("%s\n%s" % (APP_NAME, summary))
        if hasattr(self, "header_item"):
            self.header_item.set_label(
                "%s — %s" % (APP_NAME, eng.STATE_LABELS.get(overall, ""))
            )
        self._update_animation(overall == eng.SYNCING)
        if self.settings_window and self.settings_window.get_visible():
            self.settings_window.on_engine_change()
        return GLib.SOURCE_REMOVE

    def _update_animation(self, active):
        if active and not self._anim_timer:
            self._anim_timer = GLib.timeout_add(750, self._animate)
        elif not active and self._anim_timer:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None

    def _animate(self):
        self._anim_flip = not self._anim_flip
        self.tray.set_icon(
            tray.ICON_SYNCING + ("-alt" if self._anim_flip else "")
        )
        return GLib.SOURCE_CONTINUE

    # -------------------------------------------------- notifications

    def notify(self, title, body, urgency="normal"):
        if not self.config.get("notifications", True) or Notify is None:
            return
        try:
            note = Notify.Notification.new(title, body, "oso-rclone-desktop")
            levels = {
                "low": Notify.Urgency.LOW,
                "normal": Notify.Urgency.NORMAL,
                "critical": Notify.Urgency.CRITICAL,
            }
            note.set_urgency(levels.get(urgency, Notify.Urgency.NORMAL))
            note.show()
            self._notification = note
        except Exception:  # noqa: BLE001 - notifications are best-effort
            pass

    # -------------------------------------------------- windows

    def open_settings(self, page=None):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        window = self.settings_window
        window.show_all()
        window.refresh_remotes()
        window.refresh_jobs()
        window.refresh_rclone_status()
        if page is not None:
            window.notebook.set_current_page(page)
        window.present()

    # -------------------------------------------------- lifecycle

    def unlock_config(self):
        """If the rclone config is encrypted, ask once and export the password."""
        if not rclone.is_installed() or not rclone.config_is_encrypted():
            return True
        if os.environ.get("RCLONE_CONFIG_PASS"):
            return True
        password = ask_config_password(None)
        if password is None:
            self.notify(
                APP_NAME,
                "Configuration stays locked — syncing is paused until you unlock it.",
                "critical",
            )
            self.engine.set_paused(True)
            return False
        os.environ["RCLONE_CONFIG_PASS"] = password
        return True

    def run(self):
        self.unlock_config()
        self.engine.start()
        self.rebuild_menu()
        if not rclone.is_installed() or not self.config.jobs:
            GLib.timeout_add_seconds(1, self._first_run)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit)
        Gtk.main()

    def _first_run(self):
        self.open_settings(page=0 if not rclone.is_installed() else 1)
        return GLib.SOURCE_REMOVE

    def quit(self, *_args):
        try:
            self.engine.stop()
        finally:
            Gtk.main_quit()
        return False


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--version" in argv:
        print("%s %s" % (APP_NAME, __version__))
        return 0
    if "--help" in argv or "-h" in argv:
        print(
            "%s %s\n\n"
            "Usage: %s [--settings] [--sync] [--version]\n\n"
            "  --settings   open the configuration window on start\n"
            "  --sync       sync every configured folder once and exit\n"
            % (APP_NAME, __version__, APP_ID)
        )
        return 0

    util.ensure_dirs()

    if "--sync" in argv:
        return _headless_sync()

    app = Application()
    if not app.acquire_lock():
        print("%s is already running." % APP_NAME, file=sys.stderr)
        return 0
    if "--settings" in argv:
        GLib.idle_add(app.open_settings)
    app.run()
    return 0


def _headless_sync():
    """Run every job once, without a tray icon (useful for cron/systemd)."""
    import subprocess

    config = Config()
    state = State()
    engine = eng.Engine(config, state)
    rc = 0
    for job in config.jobs:
        if not job.get("enabled", True) or job.get("mode") == "mount":
            continue
        runner = eng.JobRunner(engine, job)
        argv = runner.build_command()
        print("+ " + " ".join(argv))
        proc = subprocess.run(argv)
        rc = rc or proc.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
