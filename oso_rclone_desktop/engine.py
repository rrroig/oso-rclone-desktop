"""Sync engine: one runner per configured folder pair."""

import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

from gi.repository import Gio, GLib

from . import rclone, util
from .watcher import SKIP_DIRS as _SKIP_DIRS, RecursiveWatcher

# Runner states
DISABLED = "disabled"
IDLE = "idle"
SYNCING = "syncing"
ERROR = "error"
NEEDS_RESYNC = "needs_resync"
PAUSED = "paused"
MOUNTED = "mounted"
OFFLINE = "offline"

STATE_LABELS = {
    DISABLED: "Disabled",
    IDLE: "Up to date",
    SYNCING: "Syncing…",
    ERROR: "Error",
    NEEDS_RESYNC: "First sync required",
    PAUSED: "Paused",
    MOUNTED: "Mounted",
    OFFLINE: "Waiting for network",
}

MAX_LOG_BYTES = 5 * 1024 * 1024
TAIL_LINES = 40

_PROGRESS_RE = re.compile(r"Transferred:.*?(\d+)%")
#: rclone renames the losing side of a conflict to "<name>.conflictN"
_CONFLICT_RE = re.compile(r"([^\s\x1b]+\.conflict\d+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: bisync announces "File was deleted - <relative path>" (the readable one),
#: "Queue delete - <absolute path>", and plain sync says "<file>: Skipped delete…"
_DELETE_REL_RE = re.compile(r"File was deleted\s*-?\s*(\S.*?)\s*$")
_DELETE_AFTER_RE = re.compile(r"Queue delete\s*-?\s*(\S.*?)\s*$")
_DELETE_BEFORE_RE = re.compile(
    r":\s*(.+?):\s*(?:Skipped delete as --dry-run is set|Deleted|Deleting)"
)


def extract_deletions(text, roots=()):
    """File names a (dry) run would delete, as paths relative to the synced folder.

    rclone words this three different ways depending on the command; the relative
    form is preferred, and absolute paths are trimmed back to the synced root so
    the user sees "Project/report.odt" rather than a 90-character path.
    """
    relative, absolute = [], []
    for line in _ANSI_RE.sub("", text or "").splitlines():
        match = _DELETE_REL_RE.search(line)
        if match:
            target = relative
        else:
            match = _DELETE_AFTER_RE.search(line) or _DELETE_BEFORE_RE.search(line)
            target = absolute
        if not match:
            continue
        name = match.group(1).strip()
        for root in roots:
            root = (root or "").rstrip("/")
            if root and name.startswith(root + "/"):
                name = name[len(root) + 1 :]
        if name and name not in target:
            target.append(name)
    return relative or absolute


#: rclone/bisync wording when a run is aborted by the delete guard
_SAFETY_ABORT_PATTERNS = (
    "safety abort",
    "too many deletes",
    "max delete",
    "deletes limit",
    "max-delete",
)


def _is_safety_abort(text):
    low = (text or "").lower()
    return any(p in low for p in _SAFETY_ABORT_PATTERNS)


def _rotate_log(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


class JobRunner:
    """Owns the lifecycle of a single sync job."""

    def __init__(self, engine, job):
        self.engine = engine
        self.job = job
        self.state = IDLE
        self.detail = ""
        self.progress = ""
        self.last_error = ""
        self.last_sync_ts = 0
        self.last_result = ""
        self.quota = None
        self.conflicts = []
        #: set when a run was stopped by the delete guard; holds what it wanted to remove
        self.blocked_deletions = []
        self.safety_blocked = False
        #: folders that vanished locally and need a decision before syncing
        self.pending_dir_deletions = []

        self._proc = None
        self._thread = None
        self._timer = None
        self._quota_timer = None
        self._pending = False
        self._cancelled = False
        self._watcher = None
        self._lock = threading.Lock()

        saved = engine.state.job(job["id"])
        self.last_sync_ts = saved.get("last_sync_ts", 0)
        self.last_error = saved.get("last_error", "")
        self.resync_done = saved.get("resync_done", False)

    # -------------------------------------------------- properties

    @property
    def id(self):
        return self.job["id"]

    @property
    def name(self):
        return self.job.get("name") or self.job.get("remote") or "Sync"

    @property
    def mode(self):
        return self.job.get("mode", "bisync")

    @property
    def local_path(self):
        return os.path.expanduser(self.job.get("local_path") or "")

    @property
    def remote_spec(self):
        remote = self.job.get("remote") or ""
        path = (self.job.get("remote_path") or "").strip("/")
        return "%s:%s" % (remote, path)

    @property
    def log_path(self):
        return util.job_log_path(self.id)

    @property
    def busy(self):
        return self._proc is not None and self._proc.poll() is None

    def status_text(self):
        if self.safety_blocked:
            if self.pending_dir_deletions:
                return "%d deleted folder(s) need a decision" % len(
                    self.pending_dir_deletions
                )
            count = len(self.blocked_deletions)
            return (
                "Deletion blocked — %d item(s) need your approval" % count
                if count
                else "Deletion blocked — waiting for your decision"
            )
        if self.state == SYNCING and self.progress:
            return "Syncing… %s" % self.progress
        if self.state == IDLE and self.last_sync_ts:
            return "Up to date · %s" % util.relative_time(self.last_sync_ts)
        if self.state == ERROR and self.last_error:
            return "Error: %s" % util.truncate(self.last_error, 60)
        if self.state == IDLE and self.conflicts:
            return "Up to date · %d conflict(s) to review" % len(self.conflicts)
        return STATE_LABELS.get(self.state, self.state)

    # -------------------------------------------------- lifecycle

    def start(self):
        self._configure_watcher()
        if not self.job.get("enabled", True):
            self._set_state(DISABLED)
            return
        if self.mode == "mount":
            self._start_mount()
            return
        self._set_state(PAUSED if self.engine.paused else IDLE)
        self._schedule_next()
        self._schedule_quota()

    def stop(self, wait=True):
        self._cancelled = True
        self._cancel_timers()
        if self._watcher:
            self._watcher.stop()
            self._watcher = None
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                if self.mode == "mount":
                    self._unmount()
                else:
                    proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
            if wait:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
        self._proc = None

    def reload(self, job):
        """Apply an edited configuration to a live runner."""
        self.stop()
        self.job = job
        self._cancelled = False
        self.progress = ""
        self.start()

    # -------------------------------------------------- scheduling

    def _cancel_timers(self):
        for attr in ("_timer", "_quota_timer"):
            timer = getattr(self, attr)
            if timer:
                GLib.source_remove(timer)
                setattr(self, attr, None)

    def _schedule_next(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        if self.mode == "mount" or not self.job.get("enabled", True):
            return
        minutes = max(1, int(self.job.get("interval_minutes") or 5))
        self._timer = GLib.timeout_add_seconds(minutes * 60, self._on_timer)

    def _on_timer(self):
        self._timer = None
        self.request_sync("schedule")
        self._schedule_next()
        return GLib.SOURCE_REMOVE

    def _schedule_quota(self):
        self._refresh_quota()
        if self._quota_timer:
            GLib.source_remove(self._quota_timer)
        self._quota_timer = GLib.timeout_add_seconds(900, self._on_quota_timer)

    def _on_quota_timer(self):
        self._refresh_quota()
        return GLib.SOURCE_CONTINUE

    def _refresh_quota(self):
        remote = self.job.get("remote")
        if not remote:
            return

        def worker():
            info = rclone.about(remote)
            GLib.idle_add(self._set_quota, info)

        threading.Thread(target=worker, daemon=True).start()

    def _set_quota(self, info):
        self.quota = info
        self.engine.notify_changed(self)
        return GLib.SOURCE_REMOVE

    def _configure_watcher(self):
        if self._watcher:
            self._watcher.stop()
            self._watcher = None
        if self.mode == "mount" or not self.job.get("watch", True):
            return
        if not self.job.get("enabled", True):
            return
        local = self.local_path
        if not local or not os.path.isdir(local):
            return
        self._watcher = RecursiveWatcher(
            local,
            lambda: self.request_sync("watch"),
            debounce_seconds=self.job.get("watch_debounce_seconds", 15),
        )
        self._watcher.start()

    # -------------------------------------------------- running

    def request_sync(self, reason="manual", resync=False, force=False):
        if self.mode == "mount":
            if not self.busy:
                self._start_mount()
            return
        if not self.job.get("enabled", True):
            return
        if self.engine.paused and reason != "manual":
            self._set_state(PAUSED)
            return
        if self.busy:
            self._pending = True
            return
        if not force and not self._preflight(reason):
            return
        self._launch(resync=resync, reason=reason, force=force)

    def _preflight(self, reason):
        if not rclone.is_installed():
            self._fail("rclone is not installed")
            return False
        if not self.job.get("remote"):
            self._fail("No remote configured")
            return False
        local = self.local_path
        if not local:
            self._fail("No local folder configured")
            return False
        if not os.path.isdir(local):
            try:
                os.makedirs(local, exist_ok=True)
            except OSError as exc:
                self._fail("Cannot create %s: %s" % (local, exc))
                return False
        monitor = Gio.NetworkMonitor.get_default()
        if not monitor.get_network_available():
            self._set_state(OFFLINE)
            return False
        if monitor.get_network_metered() and not self.engine.config.get(
            "sync_on_metered", True
        ):
            self.detail = "Paused on metered connection"
            self._set_state(PAUSED)
            return False
        if self.safety_blocked and reason != "force":
            return False
        known = int(self.engine.state.job(self.id).get("local_entries", 0))
        if known > 0 and self._count_local_entries() == 0:
            self._fail(
                "The local folder is empty but held %d item(s) at the last sync. "
                "Refusing to sync — is the disk or network share still mounted? "
                "If you really emptied it, use “Allow deletion” to continue." % known
            )
            self.safety_blocked = True
            self.blocked_deletions = ["(everything under %s)" % self.local_path]
            return False
        if (
            self.job.get("confirm_folder_deletions", True)
            and self.mode in ("bisync", "sync_up")
            and reason not in ("force", "resync")
        ):
            missing = self._missing_dirs()
            if missing:
                self.pending_dir_deletions = missing
                self.blocked_deletions = list(missing)
                self.safety_blocked = True
                self._fail(
                    "%d folder(s) were deleted locally. Nothing has been removed from the "
                    "remote yet — choose what should happen to them."
                    % len(missing)
                )
                return False
        if self.mode == "bisync" and not self.resync_done and reason != "resync":
            if not rclone.bisync_workdir_has_listings(local, self.remote_spec):
                self._set_state(NEEDS_RESYNC)
                return False
        return True

    MAX_TRACKED_DIRS = 5000

    def _scan_local_dirs(self):
        """Relative paths of every folder under the local root."""
        root = self.local_path
        found = set()
        if not os.path.isdir(root):
            return found
        for dirpath, dirnames, _files in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
            ]
            for name in dirnames:
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                found.add(rel)
                if len(found) >= self.MAX_TRACKED_DIRS:
                    return found
        return found

    @staticmethod
    def _topmost(paths):
        """Drop children whose parent is also missing, so we report 'Photos', not
        'Photos', 'Photos/2019', 'Photos/2019/June'."""
        result = []
        for path in sorted(paths):
            if not any(path.startswith(kept + "/") for kept in result):
                result.append(path)
        return result

    def _missing_dirs(self):
        stored = set(self.engine.state.job(self.id).get("local_dirs") or [])
        if not stored:
            return []
        return self._topmost(stored - self._scan_local_dirs())

    def _count_local_entries(self):
        try:
            with os.scandir(self.local_path) as it:
                return sum(1 for e in it if not e.name.startswith("."))
        except OSError:
            return 0

    def _launch(self, resync=False, reason="manual", force=False):
        argv = self.build_command(resync=resync, force=force)
        _rotate_log(self.log_path)
        self._cancelled = False
        self.progress = ""
        self.detail = "resync" if resync else reason
        self._set_state(SYNCING)
        if force:
            self.detail = "override"
        thread = threading.Thread(
            target=self._run_process, args=(argv, resync), daemon=True
        )
        self._thread = thread
        thread.start()

    def _run_process(self, argv, resync):
        tail = []
        started = time.time()
        try:
            with open(self.log_path, "a", buffering=1) as log:
                log.write(
                    "\n=== %s | %s | %s ===\n"
                    % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        "resync" if resync else self.mode,
                        " ".join(shlex.quote(a) for a in argv),
                    )
                )
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self._proc = proc
                for line in proc.stdout:
                    log.write(line)
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    tail.append(line)
                    if len(tail) > TAIL_LINES:
                        tail.pop(0)
                    match = _PROGRESS_RE.search(line)
                    if match:
                        GLib.idle_add(self._set_progress, match.group(1) + "%")
                    elif "Transferred:" in line:
                        GLib.idle_add(self._set_progress, util.truncate(line, 48))
                rc = proc.wait()
        except Exception as exc:  # noqa: BLE001 — never leave the runner stuck
            GLib.idle_add(self._finish, 1, ["%s" % exc], resync, 0)
            return
        finally:
            self._proc = None
        GLib.idle_add(self._finish, rc, tail, resync, time.time() - started)

    def _set_progress(self, text):
        self.progress = text
        self.engine.notify_changed(self)
        return GLib.SOURCE_REMOVE

    def _finish(self, rc, tail, resync, elapsed):
        self.progress = ""
        text = "\n".join(tail)
        if self._cancelled:
            self._set_state(IDLE)
            return GLib.SOURCE_REMOVE
        if rc in rclone.OK_CODES:
            found = self._extract_conflicts(tail)
            if found:
                self.conflicts = found
                self.engine.notify(
                    self.name,
                    "%d file(s) changed on both sides. The newer copy kept its name; "
                    "the other was saved next to it as “.conflict1”." % len(found),
                    "normal",
                )
            self.last_sync_ts = int(time.time())
            self.last_error = ""
            self.safety_blocked = False
            self.blocked_deletions = []
            saved = self.engine.state.job(self.id)
            saved["local_entries"] = self._count_local_entries()
            saved["local_dirs"] = sorted(self._scan_local_dirs())
            self.pending_dir_deletions = []
            self.last_result = self._summarise(tail)
            if resync or self.mode == "bisync":
                self.resync_done = True
            self.prune_trash()
            self._persist()
            self._set_state(IDLE)
            if self.engine.config.get("notify_on_success"):
                self.engine.notify(
                    self.name, "Sync finished in %ds" % int(elapsed), "low"
                )
        elif _is_safety_abort(text):
            self.safety_blocked = True
            self._fail(
                "Stopped on purpose: this run wanted to delete more than %d%% of the "
                "files. Nothing was deleted — approve it or undo the deletion."
                % int(self.job.get("max_delete_percent") or 25)
            )
            self.collect_blocked_deletions()
        elif rclone.output_needs_resync(text):
            self.resync_done = False
            self._persist()
            self._set_state(NEEDS_RESYNC)
            self.engine.notify(
                self.name,
                "First sync required. Open the menu and choose “Run first sync”.",
                "normal",
            )
        else:
            self._fail(self._error_line(tail) or "rclone exited with code %d" % rc)
        if self._pending:
            self._pending = False
            GLib.timeout_add_seconds(3, lambda: (self.request_sync("pending"), False)[1])
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _extract_conflicts(tail):
        names = []
        for line in tail:
            for match in _CONFLICT_RE.findall(_ANSI_RE.sub("", line)):
                name = os.path.basename(match)
                if name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _summarise(tail):
        for line in reversed(tail):
            if "Transferred:" in line or "Checks:" in line:
                return util.truncate(line, 80)
        return ""

    @staticmethod
    def _error_line(tail):
        for line in reversed(tail):
            low = line.lower()
            if "error" in low or "critical" in low or "failed" in low:
                cleaned = re.sub(r"^\S+\s+\S+\s+(ERROR|NOTICE|CRITICAL)\s*:\s*", "", line)
                return util.truncate(cleaned, 160)
        return util.truncate(tail[-1], 160) if tail else ""

    def _fail(self, message):
        self.last_error = message
        self._persist()
        self._set_state(ERROR)
        self.engine.notify(self.name, message, "critical")

    def _persist(self):
        saved = self.engine.state.job(self.id)
        saved["last_sync_ts"] = self.last_sync_ts
        saved["last_error"] = self.last_error
        saved["resync_done"] = self.resync_done
        self.engine.state.save()

    def _set_state(self, state):
        self.state = state
        self.engine.notify_changed(self)

    def cancel(self):
        proc = self._proc
        self._cancelled = True
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    # -------------------------------------------------- mount mode

    def _start_mount(self):
        if self.busy:
            return
        local = self.local_path
        if not local:
            self._fail("No mount point configured")
            return
        os.makedirs(local, exist_ok=True)
        argv = self.build_command()
        _rotate_log(self.log_path)
        self._cancelled = False
        self._set_state(SYNCING)
        threading.Thread(target=self._run_mount, args=(argv,), daemon=True).start()

    def _run_mount(self, argv):
        tail = []
        try:
            with open(self.log_path, "a", buffering=1) as log:
                log.write("\n=== %s | mount ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self._proc = proc
                GLib.timeout_add_seconds(3, self._mount_settled)
                for line in proc.stdout:
                    log.write(line)
                    tail.append(line.rstrip())
                    if len(tail) > TAIL_LINES:
                        tail.pop(0)
                rc = proc.wait()
        except Exception as exc:  # noqa: BLE001 — never leave the runner stuck
            GLib.idle_add(self._fail, str(exc))
            return
        finally:
            self._proc = None
        if not self._cancelled:
            GLib.idle_add(self._fail, self._error_line(tail) or "Mount exited (%d)" % rc)

    def _mount_settled(self):
        if self.busy and self.state == SYNCING:
            self._set_state(MOUNTED)
        return GLib.SOURCE_REMOVE

    def _unmount(self):
        for cmd in (["fusermount", "-uz", self.local_path], ["umount", self.local_path]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=10)
                return
            except (OSError, subprocess.SubprocessError):
                continue

    def collect_blocked_deletions(self, callback=None):
        """Ask rclone (dry run) exactly which files the blocked run would delete."""
        argv = self.build_command(dry_run=True, force=True)

        def worker():
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
                text = (proc.stdout or "") + (proc.stderr or "")
            except (OSError, subprocess.SubprocessError) as exc:
                text = str(exc)
            roots = (self.local_path, self.remote_spec)
            GLib.idle_add(self._set_blocked, extract_deletions(text, roots), callback)

        threading.Thread(target=worker, daemon=True).start()

    def _set_blocked(self, names, callback=None):
        if names:
            self.blocked_deletions = names
        self.engine.notify_changed(self)
        if callback:
            callback(self.blocked_deletions)
        return GLib.SOURCE_REMOVE

    def approve_deletion(self):
        """One-shot override: run the blocked sync, deletions included."""
        self.safety_blocked = False
        self.pending_dir_deletions = []
        self.last_error = ""
        self.request_sync("force", force=True)

    def stop_syncing_paths(self, paths):
        """Keep the folders on the remote, but leave them out of this pair.

        Adds an exclude rule per folder. rclone bisync treats a filter change as a
        reason to rebuild its baseline, so this runs a resync afterwards — which is
        merge-only and deletes nothing.
        """
        excludes = list(self.job.get("excludes") or [])
        for path in paths:
            path = path.strip("/")
            for rule in ("%s/**" % path, path):
                if rule not in excludes:
                    excludes.append(rule)
        self.job["excludes"] = excludes
        self.engine.config.save()
        self.safety_blocked = False
        self.pending_dir_deletions = []
        self.blocked_deletions = []
        self.last_error = ""
        self.resync_done = False
        self.request_sync("resync", resync=True, force=True)

    def restore_paths(self, paths, callback=None):
        """Bring folders back from the remote after an accidental deletion."""
        local = self.local_path
        remote = self.remote_spec

        def worker():
            errors = []
            for path in paths:
                path = path.strip("/")
                argv = [
                    rclone.binary(),
                    "copy",
                    "%s/%s" % (remote.rstrip("/"), path),
                    os.path.join(local, path),
                    "--create-empty-src-dirs",
                ]
                try:
                    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
                    if proc.returncode != 0:
                        errors.append(proc.stderr.strip().splitlines()[-1:])
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append([str(exc)])
            GLib.idle_add(self._restored, errors, callback)

        threading.Thread(target=worker, daemon=True).start()

    def _restored(self, errors, callback=None):
        if errors:
            self._fail("Restore failed: %s" % util.truncate(" ".join(sum(errors, [])), 120))
        else:
            self.safety_blocked = False
            self.pending_dir_deletions = []
            self.blocked_deletions = []
            self.last_error = ""
            self._set_state(IDLE)
            self.request_sync("manual")
        if callback:
            callback(not errors)
        return GLib.SOURCE_REMOVE

    # -------------------------------------------------- safety net

    def trash_dirs(self):
        """(local_trash, remote_trash) for --backup-dir, or None when impossible.

        Both must sit OUTSIDE the synced paths or rclone refuses to run, so the
        local trash lives under ~/.local/share and the remote one next to the
        synced folder. Syncing the very root of a Drive leaves no room for the
        remote trash, which is one more reason to sync a subfolder.
        """
        if not self.job.get("safety_backup", True):
            return None, None
        stamp = time.strftime("%Y-%m-%d")
        local_trash = os.path.join(util.TRASH_DIR, self.id, stamp)
        remote = self.job.get("remote") or ""
        remote_path = (self.job.get("remote_path") or "").strip("/")
        if not remote or not remote_path:
            return local_trash, None
        remote_trash = "%s:%s/%s/%s" % (remote, util.REMOTE_TRASH, remote_path, stamp)
        return local_trash, remote_trash

    def prune_trash(self):
        """Delete trashed copies older than the configured retention."""
        days = int(self.job.get("trash_days") or 30)
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        root = os.path.join(util.TRASH_DIR, self.id)
        if os.path.isdir(root):
            for name in os.listdir(root):
                path = os.path.join(root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        _local, remote_trash = self.trash_dirs()
        if not remote_trash:
            return
        base = remote_trash.rsplit("/", 1)[0]

        def worker():
            for args in (
                ["delete", base, "--min-age", "%dd" % days],
                ["rmdirs", base, "--leave-root"],
            ):
                try:
                    subprocess.run(
                        [rclone.binary()] + args, capture_output=True, timeout=300
                    )
                except (OSError, subprocess.SubprocessError):
                    return

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------- command building

    def build_command(self, resync=False, dry_run=False, force=False):
        job = self.job
        local = self.local_path
        remote = self.remote_spec
        modern = self.engine.modern_bisync
        argv = [rclone.binary()]

        local_trash, remote_trash = self.trash_dirs()

        mode = self.mode
        if mode == "mount":
            argv += ["mount", remote, local]
            argv += shlex.split(job.get("mount_options") or "")
        elif mode == "bisync":
            argv += ["bisync", local, remote, "--create-empty-src-dirs"]
            if modern:
                argv += [
                    "--resilient",
                    "--recover",
                    "--max-lock",
                    "2m",
                    "--conflict-resolve",
                    job.get("conflict_resolve", "newer"),
                    "--conflict-loser",
                    "num",
                ]
            # bisync reads --max-delete as a PERCENTAGE and aborts the whole run
            # rather than deleting more than that share of the files.
            if force:
                # explicit, one-off user approval: bypass the delete guard
                argv.append("--force")
            else:
                argv += ["--max-delete", str(int(job.get("max_delete_percent") or 25))]
            if local_trash:
                argv += ["--backup-dir1", local_trash]
            if remote_trash:
                argv += ["--backup-dir2", remote_trash]
            if resync:
                argv.append("--resync")
                if modern:
                    argv += ["--resync-mode", "newer"]
        elif mode in ("copy_up", "sync_up"):
            verb = "copy" if mode == "copy_up" else "sync"
            argv += [verb, local, remote, "--create-empty-src-dirs", "--track-renames"]
            if remote_trash:
                # deleted/overwritten files on Drive are moved aside, not destroyed
                argv += ["--backup-dir", remote_trash]
        elif mode in ("copy_down", "sync_down"):
            verb = "copy" if mode == "copy_down" else "sync"
            argv += [verb, remote, local, "--create-empty-src-dirs"]
            if local_trash:
                argv += ["--backup-dir", local_trash]
        else:
            argv += ["bisync", local, remote]

        # shared flags
        argv += [
            "--transfers",
            str(int(job.get("transfers") or 4)),
            "--checkers",
            str(int(job.get("checkers") or 8)),
            "--contimeout",
            "20s",
            "--timeout",
            "5m",
            "--retries",
            "3",
            "--low-level-retries",
            "10",
            "--log-level",
            "INFO",
        ]
        if mode != "mount":
            argv += ["--stats", "2s", "--stats-one-line"]
        bwlimit = (job.get("bandwidth_limit") or "").strip()
        if bwlimit:
            argv += ["--bwlimit", bwlimit]
        if job.get("skip_gdocs", True) and rclone.remote_type(job.get("remote")) == "drive":
            argv.append("--drive-skip-gdocs")
        if dry_run:
            argv.append("--dry-run")
        for pattern in [util.REMOTE_TRASH + "/**"] + list(job.get("excludes") or []):
            pattern = pattern.strip()
            if pattern and not pattern.startswith("#"):
                argv += ["--exclude", pattern]
        argv += shlex.split(job.get("extra_args") or "")
        return argv


class Engine:
    """Owns every runner plus global pause state."""

    def __init__(self, config, state, on_change=None, on_notify=None):
        self.config = config
        self.state = state
        self.runners = []
        self.paused = False
        self._on_change = on_change
        self._on_notify = on_notify
        util.ensure_dirs()
        self.modern_bisync = rclone.supports_modern_bisync()

    # -------------------------------------------------- wiring

    def notify_changed(self, runner=None):
        if self._on_change:
            self._on_change(runner)

    def notify(self, title, body, urgency="normal"):
        if self._on_notify:
            self._on_notify(title, body, urgency)

    # -------------------------------------------------- lifecycle

    def start(self):
        if self.runners:  # idempotent: never leave orphan watchers or timers behind
            self.stop()
        self.modern_bisync = rclone.supports_modern_bisync()
        self.runners = [JobRunner(self, job) for job in self.config.jobs]
        for runner in self.runners:
            runner.start()
        if self.config.get("sync_on_start", True):
            GLib.timeout_add_seconds(10, self._initial_sync)

    def _initial_sync(self):
        for runner in self.runners:
            if runner.mode != "mount":
                runner.request_sync("startup")
        return GLib.SOURCE_REMOVE

    def stop(self):
        for runner in self.runners:
            runner.stop()
        self.runners = []

    def reload(self):
        """Re-read config into the live runner set, keeping untouched jobs alive."""
        self.modern_bisync = rclone.supports_modern_bisync()
        by_id = {r.id: r for r in self.runners}
        new_runners = []
        for job in self.config.jobs:
            runner = by_id.pop(job["id"], None)
            if runner:
                runner.reload(job)
            else:
                runner = JobRunner(self, job)
                runner.start()
            new_runners.append(runner)
        for orphan in by_id.values():
            orphan.stop()
        self.runners = new_runners
        self.notify_changed(None)

    # -------------------------------------------------- actions

    def runner(self, job_id):
        for runner in self.runners:
            if runner.id == job_id:
                return runner
        return None

    def sync_all(self):
        for runner in self.runners:
            runner.request_sync("manual")

    def set_paused(self, paused):
        self.paused = paused
        for runner in self.runners:
            if runner.mode == "mount":
                continue
            if paused:
                runner.cancel()
                runner._set_state(PAUSED)
            elif runner.state == PAUSED:
                runner._set_state(IDLE)
        self.notify_changed(None)

    # -------------------------------------------------- aggregate status

    def overall_state(self):
        states = [r.state for r in self.runners if r.job.get("enabled", True)]
        if not states:
            return IDLE
        if any(s == SYNCING for s in states):
            return SYNCING
        if any(s == ERROR for s in states):
            return ERROR
        if any(s == NEEDS_RESYNC for s in states):
            return NEEDS_RESYNC
        if self.paused or all(s == PAUSED for s in states):
            return PAUSED
        if any(s == OFFLINE for s in states):
            return OFFLINE
        return IDLE
