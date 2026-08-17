"""Configuration and persisted runtime state."""

import copy
import json
import os
import tempfile
import uuid

from . import util

CONFIG_VERSION = 1

DEFAULT_EXCLUDES = [
    ".~lock.*",
    "~$*",
    "*.tmp",
    "*.partial",
    ".goutputstream-*",
    ".Trash-*/**",
    ".DS_Store",
    "Thumbs.db",
    "**/.cache/**",
]

MODES = [
    ("bisync", "Two-way sync (Dropbox-like)"),
    ("copy_up", "Upload only — never delete on Drive"),
    ("sync_up", "Mirror local → Drive (deletes on Drive)"),
    ("copy_down", "Download only — never delete locally"),
    ("sync_down", "Mirror Drive → local (deletes locally)"),
    ("mount", "Mount Drive as a folder (no local copy)"),
]

CONFLICT_CHOICES = [
    ("newer", "Keep the newer file"),
    ("older", "Keep the older file"),
    ("larger", "Keep the larger file"),
    ("smaller", "Keep the smaller file"),
    ("none", "Keep both, renamed"),
]

JOB_DEFAULTS = {
    "name": "Google Drive",
    "enabled": True,
    "remote": "",
    "remote_path": "",
    "local_path": "",
    "mode": "bisync",
    "interval_minutes": 5,
    "watch": True,
    "watch_debounce_seconds": 15,
    "bandwidth_limit": "",
    "transfers": 4,
    "checkers": 8,
    "skip_gdocs": True,
    "conflict_resolve": "newer",
    "excludes": None,  # None -> DEFAULT_EXCLUDES
    "extra_args": "",
    "mount_options": "--vfs-cache-mode writes",
}

GLOBAL_DEFAULTS = {
    "version": CONFIG_VERSION,
    "notifications": True,
    "notify_on_success": False,
    "sync_on_start": True,
    "sync_on_metered": True,  # sync while on a metered connection
    "jobs": [],
}


def new_job(**overrides):
    job = copy.deepcopy(JOB_DEFAULTS)
    job["id"] = uuid.uuid4().hex[:12]
    job["excludes"] = list(DEFAULT_EXCLUDES)
    job.update(overrides)
    return job


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Config:
    """User configuration, backed by ~/.config/oso-rclone-desktop/config.json."""

    def __init__(self, path=None):
        self.path = path or util.CONFIG_FILE
        self.data = copy.deepcopy(GLOBAL_DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        merged = copy.deepcopy(GLOBAL_DEFAULTS)
        merged.update(loaded)
        jobs = []
        for raw in merged.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            job = copy.deepcopy(JOB_DEFAULTS)
            job["id"] = raw.get("id") or uuid.uuid4().hex[:12]
            job.update({k: v for k, v in raw.items() if k in job or k == "id"})
            if not job.get("excludes"):
                job["excludes"] = list(DEFAULT_EXCLUDES)
            jobs.append(job)
        merged["jobs"] = jobs
        merged["version"] = CONFIG_VERSION
        self.data = merged

    def save(self):
        _atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=False) + "\n")

    # ---- convenience accessors

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    @property
    def jobs(self):
        return self.data.setdefault("jobs", [])

    def job(self, job_id):
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        return None

    def add_job(self, job):
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.data["jobs"] = [j for j in self.jobs if j["id"] != job_id]


class State:
    """Non-user-editable runtime state (last sync, resync bookkeeping)."""

    def __init__(self, path=None):
        self.path = path or util.STATE_FILE
        self.data = {"jobs": {}}
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.setdefault("jobs", {})
                self.data.update(loaded)
                self.data.setdefault("jobs", {})
        except (OSError, ValueError):
            pass

    def job(self, job_id):
        return self.data.setdefault("jobs", {}).setdefault(job_id, {})

    def save(self):
        try:
            _atomic_write(self.path, json.dumps(self.data, indent=2) + "\n")
        except OSError:
            pass
