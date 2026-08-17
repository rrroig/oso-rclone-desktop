"""Thin wrapper around the rclone binary."""

import json
import os
import re
import shutil
import subprocess

MIN_BISYNC_VERSION = (1, 66, 0)  # --resilient/--recover/--conflict-resolve
MIN_VERSION = (1, 58, 0)  # bisync exists at all


#: a user-level install (~/.local/bin) is common because the distro package is
#: usually too old, and a desktop session does not always inherit that PATH.
FALLBACK_PATHS = (
    os.path.expanduser("~/.local/bin/rclone"),
    "/usr/local/bin/rclone",
    "/usr/bin/rclone",
)


def binary():
    found = shutil.which("rclone")
    if found:
        return found
    for candidate in FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "rclone"


def is_installed():
    path = binary()
    return bool(shutil.which(path) or os.path.isfile(path))


def _run(args, timeout=25):
    env = dict(os.environ)
    env.setdefault("RCLONE_ASK_PASSWORD", "false")
    return subprocess.run(
        [binary()] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def version():
    """Return (tuple, raw string) or (None, '') if rclone is unusable."""
    if not is_installed():
        return None, ""
    try:
        out = _run(["version"], timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""
    match = re.search(r"rclone\s+v?(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not match:
        return None, out.strip().splitlines()[0] if out.strip() else ""
    parts = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    return parts, "v%d.%d.%d" % parts


def supports_modern_bisync():
    parts, _ = version()
    return bool(parts and parts >= MIN_BISYNC_VERSION)


def version_ok():
    parts, _ = version()
    return bool(parts and parts >= MIN_VERSION)


def listremotes():
    """List configured remotes as [(name, type), ...]."""
    if not is_installed():
        return []
    try:
        proc = _run(["config", "dump"], timeout=15)
        data = json.loads(proc.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    remotes = []
    for name, conf in sorted(data.items()):
        remotes.append((name, (conf or {}).get("type", "?")))
    return remotes


def remote_type(name):
    for remote, rtype in listremotes():
        if remote == name:
            return rtype
    return None


def about(remote):
    """Quota info for a remote: dict with used/total/free, or None."""
    if not remote:
        return None
    try:
        proc = _run(["about", "%s:" % remote, "--json"], timeout=30)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def config_path():
    """Path of the rclone configuration file (where OAuth tokens live)."""
    try:
        out = _run(["config", "file"], timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("/"):
            return line
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(cfg, "rclone", "rclone.conf")


def config_is_encrypted():
    """True when the rclone config is password-protected."""
    path = config_path()
    try:
        with open(path, "r", errors="replace") as fh:
            head = fh.read(200)
    except OSError:
        return False
    return "Encrypted rclone configuration File" in head


def config_permissions():
    """Return (path, octal_mode, world_or_group_readable)."""
    path = config_path()
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return path, None, False
    return path, mode, bool(mode & 0o077)


def harden_config_permissions():
    path, mode, loose = config_permissions()
    if mode is not None and loose:
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    return False


def check_config_password(password):
    """Validate a config password without writing anything."""
    env = dict(os.environ)
    env["RCLONE_CONFIG_PASS"] = password
    env["RCLONE_ASK_PASSWORD"] = "false"
    try:
        proc = subprocess.run(
            [binary(), "listremotes"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def config_create_argv(name, backend="drive", extra=None):
    """argv that creates a remote interactively (browser OAuth)."""
    argv = [binary(), "config", "create", name, backend]
    argv += list(extra or [])
    return argv


def config_tui_argv():
    return [binary(), "config"]


def bisync_workdir_has_listings(local_path, remote_spec):
    """True when rclone already holds bisync listings for this pair.

    rclone stores them under its cache dir, named after both paths.
    We only use this as a hint; the authoritative signal is rclone's own
    'must run --resync' error.
    """
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    workdir = os.path.join(cache, "rclone", "bisync")
    if not os.path.isdir(workdir):
        return False
    try:
        names = os.listdir(workdir)
    except OSError:
        return False
    local_key = re.sub(r"[^A-Za-z0-9]", "", local_path)[-24:]
    remote_key = re.sub(r"[^A-Za-z0-9]", "", remote_spec)[-24:]
    for name in names:
        flat = re.sub(r"[^A-Za-z0-9]", "", name)
        if local_key and remote_key and local_key in flat and remote_key in flat:
            return True
    return False


NEEDS_RESYNC_PATTERNS = (
    "must run --resync",
    "cannot find prior path1 or path2 listings",
    "run --resync to recover",
)


def output_needs_resync(text):
    low = (text or "").lower()
    return any(p in low for p in NEEDS_RESYNC_PATTERNS)


#: rclone exit codes we treat as "nothing went wrong"
OK_CODES = {0}
#: transient issues worth an automatic retry later rather than an error badge
RETRYABLE_CODES = {5, 6}
