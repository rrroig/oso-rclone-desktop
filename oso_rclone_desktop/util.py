"""Paths, small helpers, desktop integration."""

import os
import shlex
import shutil
import subprocess
import sys

from gi.repository import GLib

from . import APP_ID

# ---------------------------------------------------------------- XDG paths


def _xdg(env, default):
    return os.environ.get(env) or os.path.expanduser(default)


CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), APP_ID)
STATE_DIR = os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), APP_ID)
CACHE_DIR = os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), APP_ID)
LOG_DIR = os.path.join(STATE_DIR, "logs")
AUTOSTART_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "autostart")

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOCK_FILE = os.path.join(STATE_DIR, "instance.lock")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(PKG_DIR, "icons")


def ensure_dirs():
    for d in (CONFIG_DIR, STATE_DIR, CACHE_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def job_log_path(job_id):
    return os.path.join(LOG_DIR, "%s.log" % job_id)


# ---------------------------------------------------------------- desktop


def desktop_name():
    for var in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION"):
        val = os.environ.get(var, "")
        if val:
            return val.lower()
    return ""


def is_gnome():
    d = desktop_name()
    return "gnome" in d and "cinnamon" not in d


def is_cinnamon():
    return "cinnamon" in desktop_name()


def open_path(path):
    """Open a file or folder with the user's default handler."""
    try:
        subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


TERMINALS = [
    # (binary, args-before-command, wants a single string command)
    ("x-terminal-emulator", ["-e"], False),
    ("gnome-terminal", ["--"], False),
    ("mate-terminal", ["--"], False),
    ("xfce4-terminal", ["-x"], False),
    ("tilix", ["-e"], True),
    ("konsole", ["-e"], False),
    ("xterm", ["-e"], False),
]


def run_in_terminal(argv, title=None):
    """Launch argv inside a terminal window. Returns True if a terminal was found."""
    for binary, prefix, single_string in TERMINALS:
        path = shutil.which(binary)
        if not path:
            continue
        if single_string:
            cmd = [path] + prefix + [" ".join(shlex.quote(a) for a in argv)]
        else:
            cmd = [path] + prefix + list(argv)
        try:
            subprocess.Popen(cmd, start_new_session=True)
            return True
        except OSError:
            continue
    return False


def keep_terminal_open(argv, pause_msg="Press Enter to close this window."):
    """Wrap argv in a shell that pauses afterwards, so errors stay readable."""
    inner = " ".join(shlex.quote(a) for a in argv)
    script = "%s; echo; read -r -p %s _" % (inner, shlex.quote(pause_msg))
    return ["bash", "-lc", script]


# ---------------------------------------------------------------- autostart

AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "%s.desktop" % APP_ID)


def autostart_enabled():
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled, exec_cmd=None):
    if not enabled:
        try:
            os.remove(AUTOSTART_FILE)
        except OSError:
            pass
        return
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    exec_cmd = exec_cmd or launcher_command()
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=OSO Rclone Desktop\n"
        "Comment=Keep folders in sync with Google Drive via rclone\n"
        "Exec=%s\n"
        "Icon=oso-rclone-desktop-idle\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=8\n"
        "NoDisplay=false\n" % exec_cmd
    )
    with open(AUTOSTART_FILE, "w") as fh:
        fh.write(content)


def launcher_command():
    """Best-effort command line that re-launches this app."""
    installed = shutil.which("oso-rclone-desktop")
    if installed:
        return installed
    return "%s -m oso_rclone_desktop" % shlex.quote(sys.executable)


# ---------------------------------------------------------------- misc


def human_size(num):
    if num is None:
        return "?"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(num)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % value
        value /= 1024.0
    return "%.1f PiB" % value


def relative_time(ts):
    """'hace 3 min' style label from a unix timestamp."""
    if not ts:
        return "never"
    delta = max(0, int(GLib.get_real_time() / 1e6 - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%d min ago" % (delta // 60)
    if delta < 86400:
        return "%d h ago" % (delta // 3600)
    return "%d d ago" % (delta // 86400)


def truncate(text, limit=90):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
