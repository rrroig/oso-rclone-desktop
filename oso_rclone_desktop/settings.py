"""Configuration window: accounts, synced folders, logs, general options."""

import json
import os
import re
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import APP_NAME, __version__, config as cfgmod, engine as eng, rclone, util  # noqa: E402

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

INSTALL_RCLONE_CMD = "curl -fsSL https://rclone.org/install.sh | sudo bash"

PROJECT_URL = "https://github.com/rrroig/oso-rclone-desktop"
ISSUES_URL = PROJECT_URL + "/issues"

STATE_ICONS = {
    eng.IDLE: "emblem-default",
    eng.SYNCING: "emblem-synchronizing",
    eng.ERROR: "dialog-error",
    eng.NEEDS_RESYNC: "dialog-warning",
    eng.PAUSED: "media-playback-pause",
    eng.DISABLED: "action-unavailable",
    eng.MOUNTED: "drive-harddisk",
    eng.OFFLINE: "network-offline",
}


def _label(text, bold=False, dim=False, wrap=False):
    lbl = Gtk.Label(label=text, xalign=0.0)
    if bold:
        lbl.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
    if dim:
        lbl.get_style_context().add_class("dim-label")
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(60)
    return lbl


def _message(parent, kind, title, body=None):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=kind,
        buttons=Gtk.ButtonsType.OK,
        text=title,
    )
    if body:
        dialog.format_secondary_text(body)
    dialog.run()
    dialog.destroy()


def _ask_text(parent, title, prompt, default=""):
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
    )
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_spacing(8)
    box.set_border_width(12)
    box.add(_label(prompt))
    entry = Gtk.Entry(text=default)
    entry.set_activates_default(True)
    box.add(entry)
    box.show_all()
    response = dialog.run()
    value = entry.get_text().strip()
    dialog.destroy()
    return value if response == Gtk.ResponseType.OK else None


def _confirm(parent, title, body):
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.OK_CANCEL,
        text=title,
    )
    dialog.format_secondary_text(body)
    result = dialog.run() == Gtk.ResponseType.OK
    dialog.destroy()
    return result


def ask_config_password(parent=None):
    """Prompt for the rclone config password. Returns the password or None."""
    dialog = Gtk.Dialog(title="Unlock rclone configuration", transient_for=parent, modal=True)
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "_Unlock", Gtk.ResponseType.OK
    )
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_spacing(8)
    box.set_border_width(14)
    box.add(
        _label(
            "Your rclone configuration is encrypted.\n"
            "Enter its password to unlock the stored account tokens.",
            wrap=True,
        )
    )
    entry = Gtk.Entry(visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD)
    entry.set_activates_default(True)
    box.add(entry)
    error = _label("", dim=True)
    box.add(error)
    box.show_all()

    while True:
        if dialog.run() != Gtk.ResponseType.OK:
            dialog.destroy()
            return None
        password = entry.get_text()
        if rclone.check_config_password(password):
            dialog.destroy()
            return password
        error.set_markup("<span foreground='red'>Wrong password — try again.</span>")


DRIVE_SCOPES = [
    (
        "drive",
        "Full access to my Drive",
        "Read, change and delete anything in the Drive. Required for two-way sync of "
        "folders that already contain files, because rclone has to see them.",
    ),
    (
        "drive.file",
        "Only files this app creates",
        "Google hides everything else from rclone entirely — it cannot even list the "
        "rest of your Drive. Works when the Drive folder starts empty and is only ever "
        "filled through this app; files you later add from the Drive website stay "
        "invisible and will not come down.",
    ),
    (
        "drive.readonly",
        "Read-only",
        "Can download but never modify or delete anything on Drive. Use it with the "
        "download-only sync modes.",
    ),
]

CLIENT_ID_HELP = (
    "rclone ships a shared client ID, so the Google consent screen says “rclone” and "
    "everybody using rclone shares the same API quota, which Google throttles. Creating "
    "your own client ID in Google Cloud Console (free) fixes both: the consent screen "
    "shows your own project, the quota is yours alone, and you can revoke it yourself. "
    "Either way the token is only ever stored on this computer.\n\n"
    "One catch worth knowing: if you leave your Google Cloud project in “Testing” "
    "publishing status, Google expires the refresh token after 7 days and you have to "
    "sign in again every week. Set the OAuth consent screen to “In production” — for "
    "your own personal use it can stay unverified, you just click past a warning once."
)


class ConnectAccountDialog(Gtk.Dialog):
    """Ask what access to request before sending the user to Google."""

    def __init__(self, parent, default_name):
        super().__init__(title="Connect Google Drive", transient_for=parent, modal=True)
        self.set_default_size(620, 100)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        connect = self.add_button("Sign in with Google", Gtk.ResponseType.OK)
        connect.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        box = self.get_content_area()
        box.set_border_width(14)
        box.set_spacing(10)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.add(grid)

        name_label = _label("Account name")
        name_label.set_halign(Gtk.Align.END)
        grid.attach(name_label, 0, 0, 1, 1)
        self.name_entry = Gtk.Entry(text=default_name)
        self.name_entry.set_hexpand(True)
        grid.attach(self.name_entry, 1, 0, 1, 1)

        scope_label = _label("Access to request")
        scope_label.set_halign(Gtk.Align.END)
        grid.attach(scope_label, 0, 1, 1, 1)
        self.scope_combo = Gtk.ComboBoxText()
        for key, label, _help in DRIVE_SCOPES:
            self.scope_combo.append(key, label)
        self.scope_combo.set_active_id("drive")
        self.scope_combo.connect("changed", self._on_scope_changed)
        grid.attach(self.scope_combo, 1, 1, 1, 1)

        self.scope_help = _label("", dim=True, wrap=True)
        box.add(self.scope_help)
        self._on_scope_changed(None)

        box.add(
            _label(
                "Sign-in happens in your browser and this app never sees your password. "
                "Google grants a token to rclone running on this computer; nothing is sent "
                "to any third-party server. You can withdraw it at any time from your "
                "Google account's security settings.",
                dim=True,
                wrap=True,
            )
        )

        advanced = Gtk.Expander(label="Use my own Google client ID (recommended)")
        box.add(advanced)
        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        advanced.add(adv_box)
        adv_box.add(_label(CLIENT_ID_HELP, dim=True, wrap=True))
        link = Gtk.LinkButton.new_with_label(
            "https://rclone.org/drive/#making-your-own-client-id",
            "How to create one (rclone documentation)",
        )
        link.connect(
            "activate-link",
            lambda *_a: (
                util.open_url("https://rclone.org/drive/#making-your-own-client-id"),
                True,
            )[1],
        )
        adv_box.add(link)
        adv_grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        adv_box.add(adv_grid)
        self.client_id = Gtk.Entry(placeholder_text="client ID (optional)")
        self.client_secret = Gtk.Entry(placeholder_text="client secret (optional)")
        for row, (text, widget) in enumerate(
            (("Client ID", self.client_id), ("Client secret", self.client_secret))
        ):
            label = _label(text)
            label.set_halign(Gtk.Align.END)
            adv_grid.attach(label, 0, row, 1, 1)
            widget.set_hexpand(True)
            adv_grid.attach(widget, 1, row, 1, 1)

        box.show_all()

    def _on_scope_changed(self, _combo):
        key = self.scope_combo.get_active_id()
        for scope, _label_text, help_text in DRIVE_SCOPES:
            if scope == key:
                self.scope_help.set_text(help_text)
                return

    def values(self):
        options = ["scope=%s" % (self.scope_combo.get_active_id() or "drive")]
        client_id = self.client_id.get_text().strip()
        secret = self.client_secret.get_text().strip()
        if client_id:
            options.append("client_id=%s" % client_id)
        if secret:
            options.append("client_secret=%s" % secret)
        return self.name_entry.get_text().strip(), options


class DryRunDialog(Gtk.Dialog):
    """Show exactly what a sync would do, without touching a single file."""

    def __init__(self, parent, runner):
        super().__init__(
            title="Preview — %s" % runner.name, transient_for=parent, modal=True
        )
        self.runner = runner
        self.set_default_size(760, 520)
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)

        box = self.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)
        box.add(
            _label(
                "This is a dry run: rclone reports what it would copy, move or delete and "
                "changes nothing. Read it before the first real sync.",
                wrap=True,
            )
        )
        self.summary = _label("Running…", bold=False, dim=True, wrap=True)
        box.add(self.summary)

        self.view = Gtk.TextView(editable=False, monospace=True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        self.connect("response", lambda *_a: self.destroy())
        self.show_all()
        self._run()

    def _run(self):
        argv = self.runner.build_command(
            resync=not self.runner.resync_done and self.runner.mode == "bisync",
            dry_run=True,
        )
        self.view.get_buffer().set_text("$ %s\n\n" % " ".join(argv))

        def worker():
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=600
                )
                output = (proc.stdout or "") + (proc.stderr or "")
                rc = proc.returncode
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                output, rc = str(exc), 1
            GLib.idle_add(self._done, output, rc)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, output, rc):
        clean = _ANSI.sub("", output)
        buf = self.view.get_buffer()
        buf.insert(buf.get_end_iter(), clean)
        deletes = len(re.findall(r"(?m)^.*\bDeleted\b.*$", clean)) + len(
            re.findall(r"(?m)^.*Skipped delete.*$", clean)
        )
        copies = len(re.findall(r"(?m)^.*Skipped copy.*$", clean)) + len(
            re.findall(r"(?m)^.*\bCopied\b.*$", clean)
        )
        verdict = "finished" if rc == 0 else "reported an error (exit %d)" % rc
        self.summary.set_markup(
            "<b>Dry run %s — about %d file(s) would be transferred and %d deleted. "
            "Nothing was changed.</b>" % (verdict, copies, deletes)
        )
        return GLib.SOURCE_REMOVE


class BlockedDeletionsDialog(Gtk.Dialog):
    """What to do about a deletion the safety net stopped.

    Two cases land here: a run that would delete more than the allowed share of
    the files, and — separately — folders that disappeared locally, which always
    ask, because "I deleted a folder" and "I no longer want that folder synced"
    look identical on disk and mean very different things.
    """

    RESPONSE_DELETE = 1
    RESPONSE_UNSYNC = 2
    RESPONSE_RESTORE = 3
    RESPONSE_OPEN = 4

    def __init__(self, parent, runner):
        super().__init__(
            title="Deletion needs your approval — %s" % runner.name,
            transient_for=parent,
            modal=True,
        )
        self.runner = runner
        self.folders = list(runner.pending_dir_deletions)
        self.set_default_size(660, 480)

        box = self.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)

        if self.folders:
            intro = (
                "These folders were deleted from the local copy. Nothing has been removed "
                "from the remote yet.\n\n"
                "• <b>Delete them everywhere</b> — the deletion is applied to the remote too. "
                "Copies go to the trash folder first, and on Google Drive to Drive's own bin, "
                "so it stays reversible.\n"
                "• <b>Keep them, stop syncing</b> — the folders stay untouched on the remote "
                "and are simply excluded from this pair from now on.\n"
                "• <b>Restore them here</b> — you deleted them by mistake; download them back."
            )
        else:
            intro = (
                "A sync wanted to delete more than the allowed share of the files, so it was "
                "cancelled and <b>nothing has been deleted</b>.\n\n"
                "If you meant it, approve it and the deletion is applied to the other side, "
                "with copies kept in the trash folder. If this was an accident — wrong folder, "
                "disk not mounted — close this window and the next sync restores the files."
            )
        label = Gtk.Label(xalign=0.0)
        label.set_markup(intro)
        label.set_line_wrap(True)
        label.set_max_width_chars(72)
        box.add(label)

        self.store = Gtk.ListStore(str)
        view = Gtk.TreeView(model=self.store, headers_visible=False)
        view.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)

        self.count_label = _label("", dim=True)
        box.pack_start(self.count_label, False, False, 0)

        self.add_button("Open folder", self.RESPONSE_OPEN)
        self.add_button("Decide later", Gtk.ResponseType.CANCEL)
        if self.folders:
            self.add_button("Restore them here", self.RESPONSE_RESTORE)
            self.add_button("Keep them, stop syncing", self.RESPONSE_UNSYNC)
        self.delete_btn = self.add_button(
            "Delete them everywhere" if self.folders else "Delete these files",
            self.RESPONSE_DELETE,
        )
        self.delete_btn.get_style_context().add_class("destructive-action")

        self.connect("response", self._on_response)
        self.show_all()

        if self.folders:
            self._fill(self.folders)
        else:
            self.count_label.set_text("Checking what would be deleted…")
            self.delete_btn.set_sensitive(False)
            self._fill(runner.blocked_deletions)
            runner.collect_blocked_deletions(callback=self._fill)

    def _fill(self, names):
        self.store.clear()
        for name in names or []:
            self.store.append([name])
        if names:
            self.count_label.set_text(
                "%d folder(s)." % len(names)
                if self.folders
                else "%d item(s) would be deleted." % len(names)
            )
            self.delete_btn.set_sensitive(True)
        return False

    def _on_response(self, _dialog, response):
        if response == self.RESPONSE_OPEN:
            util.open_path(self.runner.local_path)
            return
        if response == self.RESPONSE_DELETE:
            if not _confirm(
                self,
                "Delete %d item(s) on the other side?" % len(self.store),
                "This applies a deletion you already made locally. Copies are kept in the "
                "trash folder for the retention period, so it can still be undone.",
            ):
                return
            self.runner.approve_deletion()
        elif response == self.RESPONSE_UNSYNC:
            self.runner.stop_syncing_paths(self.folders)
            _message(
                self,
                Gtk.MessageType.INFO,
                "Kept on the remote, no longer synced",
                "Exclude rules were added for %d folder(s). They stay exactly as they are "
                "on the remote. Remove the rules under Advanced → Exclude patterns if you "
                "change your mind." % len(self.folders),
            )
        elif response == self.RESPONSE_RESTORE:
            self.count_label.set_text("Downloading…")
            self.set_sensitive(False)
            self.runner.restore_paths(self.folders, callback=lambda ok: self.destroy())
            return
        self.destroy()


class ConflictsDialog(Gtk.Dialog):
    """List the files rclone had to keep twice after a two-way conflict."""

    def __init__(self, parent, runner):
        super().__init__(
            title="Sync conflicts — %s" % runner.name, transient_for=parent, modal=True
        )
        self.runner = runner
        self.set_default_size(560, 380)
        self.add_buttons("Open folder", 1, "Mark as reviewed", 2, Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)

        box = self.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)
        box.add(
            _label(
                "These files were changed on both sides between two syncs. rclone kept the "
                "newer copy under its original name and saved the other one alongside it "
                "with a “.conflictN” suffix — nothing was discarded. Compare them, keep what "
                "you want and delete the rest.",
                wrap=True,
            )
        )
        store = Gtk.ListStore(str)
        for name in runner.conflicts:
            store.append([name])
        view = Gtk.TreeView(model=store, headers_visible=False)
        view.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)
        self.connect("response", self._on_response)
        self.show_all()

    def _on_response(self, _dialog, response):
        if response == 1:
            util.open_path(self.runner.local_path)
            return
        if response == 2:
            self.runner.conflicts = []
            self.runner.engine.notify_changed(self.runner)
        self.destroy()


# --------------------------------------------------------------------------- remote browser


class RemoteBrowser(Gtk.Dialog):
    """Pick (or create) a folder inside an rclone remote."""

    def __init__(self, parent, remote, start_path=""):
        super().__init__(title="Choose a folder in %s:" % remote, transient_for=parent, modal=True)
        self.remote = remote
        self.path = (start_path or "").strip("/")
        self.set_default_size(520, 420)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK
        )

        box = self.get_content_area()
        box.set_border_width(10)
        box.set_spacing(8)

        bar = Gtk.Box(spacing=6)
        self.up_btn = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        self.up_btn.connect("clicked", self._on_up)
        bar.pack_start(self.up_btn, False, False, 0)
        self.path_label = _label("", dim=True)
        bar.pack_start(self.path_label, True, True, 0)
        new_btn = Gtk.Button(label="New folder")
        new_btn.connect("clicked", self._on_new)
        bar.pack_start(new_btn, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.store = Gtk.ListStore(str)
        self.view = Gtk.TreeView(model=self.store, headers_visible=False)
        column = Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0)
        self.view.append_column(column)
        self.view.connect("row-activated", self._on_activate)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        self.spinner = Gtk.Label(label="Loading…", xalign=0.0)
        box.pack_start(self.spinner, False, False, 0)

        self.show_all()
        self._reload()

    def _spec(self):
        return "%s:%s" % (self.remote, self.path)

    def _reload(self):
        self.path_label.set_text("/" + self.path)
        self.store.clear()
        self.spinner.set_text("Loading…")

        def worker():
            try:
                proc = subprocess.run(
                    [rclone.binary(), "lsjson", "--dirs-only", self._spec()],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                names = (
                    sorted(item["Name"] for item in json.loads(proc.stdout or "[]"))
                    if proc.returncode == 0
                    else []
                )
                error = "" if proc.returncode == 0 else proc.stderr.strip().splitlines()[-1:]
            except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
                names, error = [], [str(exc)]
            GLib.idle_add(self._fill, names, " ".join(error))

        threading.Thread(target=worker, daemon=True).start()

    def _fill(self, names, error):
        for name in names:
            self.store.append([name])
        self.spinner.set_text(error or ("%d folders" % len(names)))
        return GLib.SOURCE_REMOVE

    def _on_activate(self, _view, path, _column):
        name = self.store[path][0]
        self.path = ("%s/%s" % (self.path, name)).strip("/")
        self._reload()

    def _on_up(self, _btn):
        self.path = self.path.rsplit("/", 1)[0] if "/" in self.path else ""
        self._reload()

    def _on_new(self, _btn):
        name = _ask_text(self, "New folder", "Folder name:")
        if not name:
            return
        target = "%s/%s" % (self._spec(), name)
        subprocess.run([rclone.binary(), "mkdir", target], capture_output=True, timeout=60)
        self._reload()

    def selected_path(self):
        model, treeiter = self.view.get_selection().get_selected()
        if treeiter:
            return ("%s/%s" % (self.path, model[treeiter][0])).strip("/")
        return self.path


# --------------------------------------------------------------------------- main window


class SettingsWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="%s — Settings" % APP_NAME)
        self.app = app
        self.config = app.config
        self.engine = app.engine
        self._loading = False
        self._current_job_id = None
        self._log_timer = None

        self.set_default_size(920, 640)
        self.set_icon_name("oso-rclone-desktop")
        self.connect("delete-event", self._on_close)

        header = Gtk.HeaderBar(title=APP_NAME, subtitle="Google Drive sync via rclone")
        header.set_show_close_button(True)
        sync_btn = Gtk.Button(label="Sync now")
        sync_btn.connect("clicked", lambda *_a: self.engine.sync_all())
        header.pack_end(sync_btn)
        self.set_titlebar(header)

        self.notebook = Gtk.Notebook()
        self.notebook.set_border_width(0)
        self.add(self.notebook)

        self.notebook.append_page(self._build_accounts(), Gtk.Label(label="Accounts"))
        self.notebook.append_page(self._build_folders(), Gtk.Label(label="Synced folders"))
        self.notebook.append_page(self._build_logs(), Gtk.Label(label="Logs"))
        self.notebook.append_page(self._build_general(), Gtk.Label(label="General"))
        self.notebook.connect("switch-page", self._on_switch_page)

        self.refresh_remotes()
        self.refresh_jobs()
        self.refresh_rclone_status()
        self.refresh_security()

    # ------------------------------------------------------------ accounts tab

    def _build_accounts(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)

        self.rclone_bar = Gtk.InfoBar()
        self.rclone_bar.set_show_close_button(False)
        self.rclone_label = _label("", wrap=True)
        self.rclone_bar.get_content_area().add(self.rclone_label)
        self.rclone_install_btn = self.rclone_bar.add_button("Install rclone…", 1)
        self.rclone_bar.connect("response", lambda *_a: self._install_rclone())
        outer.pack_start(self.rclone_bar, False, False, 0)

        outer.pack_start(_label("Connected accounts (rclone remotes)", bold=True), False, False, 0)
        outer.pack_start(
            _label(
                "Each account is an rclone remote. “Connect Google Drive” opens a terminal and "
                "your browser to authorise the account; the token is stored in ~/.config/rclone.",
                dim=True,
                wrap=True,
            ),
            False,
            False,
            0,
        )

        self.remote_store = Gtk.ListStore(str, str, str, str)  # name, type, access, quota
        self.remote_view = Gtk.TreeView(model=self.remote_store)
        for idx, title in enumerate(("Account", "Type", "Access granted", "Usage")):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=idx)
            column.set_resizable(True)
            if idx == 0:
                column.set_min_width(180)
            self.remote_view.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.remote_view)
        outer.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        add_btn = Gtk.Button(label="Connect Google Drive…")
        add_btn.get_style_context().add_class("suggested-action")
        add_btn.connect("clicked", self._on_add_drive)
        buttons.pack_start(add_btn, False, False, 0)
        for label, handler in (
            ("Other provider…", self._on_config_tui),
            ("Restrict to folder…", self._on_restrict),
            ("Reconnect", self._on_reconnect),
            ("Test", self._on_test_remote),
            ("Remove", self._on_remove_remote),
            ("Refresh", lambda *_a: self.refresh_remotes()),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            buttons.pack_start(btn, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

        outer.pack_start(Gtk.Separator(), False, False, 4)
        outer.pack_start(_label("Credential security", bold=True), False, False, 0)
        self.security_label = _label("", dim=True, wrap=True)
        outer.pack_start(self.security_label, False, False, 0)
        sec_buttons = Gtk.Box(spacing=6)
        for label, handler in (
            ("Set config password…", self._on_encrypt_config),
            ("Fix file permissions", self._on_fix_permissions),
            ("Open rclone config", self._on_open_config),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            sec_buttons.pack_start(btn, False, False, 0)
        outer.pack_start(sec_buttons, False, False, 0)
        return outer

    def refresh_security(self):
        path, mode, loose = rclone.config_permissions()
        encrypted = rclone.config_is_encrypted()
        bits = [
            "Tokens are stored by rclone in %s" % path,
            "Encryption: %s"
            % (
                "on — the file is unreadable without the password"
                if encrypted
                else "off — anyone who can read the file can use your accounts"
            ),
            "Permissions: %s%s"
            % (
                oct(mode)[2:] if mode is not None else "file not created yet",
                "  ⚠ readable by other users" if loose else "",
            ),
        ]
        bits.append(
            "OSO Rclone Desktop never sees your Google password: sign-in happens in your "
            "browser and only an OAuth token is stored."
        )
        self.security_label.set_text("\n".join(bits))

    def _on_encrypt_config(self, _btn):
        _message(
            self,
            Gtk.MessageType.INFO,
            "Set a password for the rclone config",
            "A terminal will open with rclone's configuration menu.\n"
            "Choose “s) Set configuration password”, then “a) Add password”.\n\n"
            "Afterwards this app asks for that password once per session.",
        )
        util.run_in_terminal(util.keep_terminal_open(rclone.config_tui_argv()))

    def _on_fix_permissions(self, _btn):
        changed = rclone.harden_config_permissions()
        self.refresh_security()
        _message(
            self,
            Gtk.MessageType.INFO,
            "Permissions set to 600" if changed else "Nothing to change",
            "Only your user can read the rclone configuration."
            if changed
            else "The file was already private (or does not exist yet).",
        )

    def _on_open_config(self, _btn):
        util.open_path(os.path.dirname(rclone.config_path()))

    def refresh_rclone_status(self):
        parts, raw = rclone.version()
        if not rclone.is_installed():
            self.rclone_bar.set_message_type(Gtk.MessageType.ERROR)
            self.rclone_label.set_text(
                "rclone is not installed. Install it to start syncing."
            )
            self._set_install_button(True)
        elif not parts or parts < rclone.MIN_BISYNC_VERSION:
            self.rclone_bar.set_message_type(Gtk.MessageType.WARNING)
            self.rclone_label.set_text(
                "rclone %s found. Two-way sync needs 1.66 or newer — the distro package is "
                "usually too old. Installing the official build is recommended." % (raw or "?")
            )
            self._set_install_button(True)
        else:
            self.rclone_bar.set_message_type(Gtk.MessageType.INFO)
            self.rclone_label.set_text("rclone %s — ready." % raw)
            self._set_install_button(False)
        self.rclone_bar.show()

    def _set_install_button(self, visible):
        self.rclone_install_btn.set_no_show_all(not visible)
        self.rclone_install_btn.set_visible(visible)

    def refresh_remotes(self):
        selected = self.selected_remote()
        self.remote_store.clear()
        for name, rtype in rclone.listremotes():
            self.remote_store.append([name, rtype, rclone.access_summary(name), "…"])
        self._refresh_remote_combo()
        if selected:
            for row in self.remote_store:
                if row[0] == selected:
                    self.remote_view.get_selection().select_iter(row.iter)
                    break

        def worker():
            results = {}
            for name, _rtype in rclone.listremotes():
                info = rclone.about(name)
                if info:
                    used = util.human_size(info.get("used"))
                    total = info.get("total")
                    results[name] = "%s of %s" % (used, util.human_size(total)) if total else used
                else:
                    results[name] = "—"
            GLib.idle_add(self._fill_quota, results)

        threading.Thread(target=worker, daemon=True).start()

    def _fill_quota(self, results):
        for row in self.remote_store:
            row[3] = results.get(row[0], "—")
        return GLib.SOURCE_REMOVE

    def selected_remote(self):
        model, treeiter = self.remote_view.get_selection().get_selected()
        return model[treeiter][0] if treeiter else None

    def _install_rclone(self):
        if not util.run_in_terminal(util.keep_terminal_open(["bash", "-lc", INSTALL_RCLONE_CMD])):
            _message(
                self,
                Gtk.MessageType.WARNING,
                "No terminal found",
                "Run this in a terminal:\n\n%s" % INSTALL_RCLONE_CMD,
            )
            return
        GLib.timeout_add_seconds(5, self._poll_rclone_install)

    def _poll_rclone_install(self):
        self.refresh_rclone_status()
        self.refresh_remotes()
        return GLib.SOURCE_REMOVE

    def _on_add_drive(self, _btn):
        if not rclone.is_installed():
            _message(self, Gtk.MessageType.ERROR, "rclone is not installed")
            return
        existing = {name for name, _t in rclone.listremotes()}
        default = "gdrive"
        index = 2
        while default in existing:
            default = "gdrive%d" % index
            index += 1
        dialog = ConnectAccountDialog(self, default)
        response = dialog.run()
        name, options = dialog.values()
        dialog.destroy()
        if response != Gtk.ResponseType.OK or not name:
            return
        if name in existing:
            _message(self, Gtk.MessageType.ERROR, "A remote named “%s” already exists." % name)
            return
        argv = rclone.config_create_argv(name, "drive", options)
        browser = self.config.get("auth_browser", "")
        if not util.run_in_terminal(util.keep_terminal_open(argv, browser=browser)):
            _message(
                self,
                Gtk.MessageType.WARNING,
                "No terminal found",
                "Run this command manually:\n\n%s" % " ".join(argv),
            )
            return
        browser_label = dict(util.detected_browsers()).get(browser, browser or "")
        _message(
            self,
            Gtk.MessageType.INFO,
            "Authorise in %s" % (browser_label or "your browser"),
            "A terminal opened and %s should ask you to sign in to Google.\n"
            "When it finishes, close the terminal and press Refresh.\n\n"
            "Wrong browser? Change it under General → Sign-in browser."
            % (browser_label or "your default browser"),
        )
        GLib.timeout_add_seconds(10, lambda: (self.refresh_remotes(), False)[1])

    def _on_config_tui(self, _btn):
        browser = self.config.get("auth_browser", "")
        if not util.run_in_terminal(
            util.keep_terminal_open(rclone.config_tui_argv(), browser=browser)
        ):
            _message(self, Gtk.MessageType.WARNING, "No terminal found", "Run: rclone config")

    def _on_restrict(self, _btn):
        remote = self.selected_remote()
        if not remote:
            return
        if rclone.remote_type(remote) != "drive":
            _message(self, Gtk.MessageType.INFO, "Only Google Drive accounts can be restricted")
            return
        _message(
            self,
            Gtk.MessageType.INFO,
            "Pick the only folder this account may touch",
            "Google has no per-folder permission, so the sign-in always grants access to "
            "the whole Drive. This pins rclone itself to one folder: from then on it "
            "cannot see or change anything outside it, whatever this app asks for.",
        )
        browser = RemoteBrowser(self, remote)
        chosen = None
        if browser.run() == Gtk.ResponseType.OK:
            chosen = browser.selected_path()
        browser.destroy()
        if not chosen:
            return
        folder_id = rclone.folder_id(remote, chosen)
        if not folder_id:
            _message(
                self,
                Gtk.MessageType.ERROR,
                "Could not read the folder id",
                "rclone did not return an id for “%s”." % chosen,
            )
            return
        if not rclone.set_root_folder(remote, folder_id):
            _message(self, Gtk.MessageType.ERROR, "rclone refused to update the account")
            return
        _message(
            self,
            Gtk.MessageType.INFO,
            "“%s” is now the root of %s" % (chosen, remote),
            "Paths in this app are now relative to that folder, so leave “Folder in Drive” "
            "empty to sync it whole. Undo it with: rclone config update %s root_folder_id \"\""
            % remote,
        )
        self.refresh_remotes()

    def _on_reconnect(self, _btn):
        remote = self.selected_remote()
        if not remote:
            return
        argv = [rclone.binary(), "config", "reconnect", "%s:" % remote]
        util.run_in_terminal(
            util.keep_terminal_open(argv, browser=self.config.get("auth_browser", ""))
        )

    def _on_test_remote(self, _btn):
        remote = self.selected_remote()
        if not remote:
            return
        info = rclone.about(remote)
        if info:
            _message(
                self,
                Gtk.MessageType.INFO,
                "%s: works" % remote,
                "Used %s of %s"
                % (util.human_size(info.get("used")), util.human_size(info.get("total"))),
            )
        else:
            _message(
                self,
                Gtk.MessageType.ERROR,
                "%s: no answer" % remote,
                "rclone could not query this remote. Try Reconnect.",
            )

    def _on_remove_remote(self, _btn):
        remote = self.selected_remote()
        if not remote:
            return
        used_by = [j["name"] for j in self.config.jobs if j.get("remote") == remote]
        body = "The rclone remote will be deleted. Local files are not touched."
        if used_by:
            body += "\n\nStill used by: %s" % ", ".join(used_by)
        if not _confirm(self, "Remove remote “%s”?" % remote, body):
            return
        subprocess.run([rclone.binary(), "config", "delete", remote], capture_output=True)
        self.refresh_remotes()

    # ------------------------------------------------------------ folders tab

    def _build_folders(self):
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(250)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_border_width(10)
        self.job_store = Gtk.ListStore(str, str, str)  # id, name, icon
        self.job_view = Gtk.TreeView(model=self.job_store, headers_visible=False)
        icon_col = Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), icon_name=2)
        self.job_view.append_column(icon_col)
        self.job_view.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=1))
        self.job_view.get_selection().connect("changed", self._on_job_selected)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.job_view)
        left.pack_start(scroller, True, True, 0)

        btns = Gtk.Box(spacing=4)
        add = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        add.set_tooltip_text("Add a synced folder")
        add.connect("clicked", self._on_add_job)
        remove = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
        remove.set_tooltip_text("Remove the selected folder")
        remove.connect("clicked", self._on_remove_job)
        btns.pack_start(add, False, False, 0)
        btns.pack_start(remove, False, False, 0)
        left.pack_start(btns, False, False, 0)
        paned.pack1(left, False, False)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.job_form = self._build_job_form()
        right_scroll.add(self.job_form)
        paned.pack2(right_scroll, True, False)
        return paned

    def _row(self, grid, row, label, widget, tooltip=None):
        lbl = _label(label)
        lbl.set_halign(Gtk.Align.END)
        grid.attach(lbl, 0, row, 1, 1)
        widget.set_hexpand(True)
        if tooltip:
            widget.set_tooltip_text(tooltip)
        grid.attach(widget, 1, row, 1, 1)
        return widget

    def _build_job_form(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(14)

        self.job_status = _label("", dim=True, wrap=True)
        box.pack_start(self.job_status, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.pack_start(grid, False, False, 0)
        row = 0

        self.f_name = self._row(grid, row, "Name", Gtk.Entry())
        row += 1

        self.f_enabled = Gtk.CheckButton(label="Keep this folder in sync")
        grid.attach(self.f_enabled, 1, row, 1, 1)
        row += 1

        self.f_remote = Gtk.ComboBoxText()
        self._row(grid, row, "Account", self.f_remote)
        row += 1

        remote_box = Gtk.Box(spacing=6)
        self.f_remote_path = Gtk.Entry(placeholder_text="(top level of the Drive)")
        remote_box.pack_start(self.f_remote_path, True, True, 0)
        browse_remote = Gtk.Button(label="Browse…")
        browse_remote.connect("clicked", self._on_browse_remote)
        remote_box.pack_start(browse_remote, False, False, 0)
        self._row(grid, row, "Folder in Drive", remote_box)
        row += 1

        local_box = Gtk.Box(spacing=6)
        self.f_local = Gtk.Entry(placeholder_text=os.path.expanduser("~/Google Drive"))
        local_box.pack_start(self.f_local, True, True, 0)
        browse_local = Gtk.Button(label="Browse…")
        browse_local.connect("clicked", self._on_browse_local)
        local_box.pack_start(browse_local, False, False, 0)
        self._row(grid, row, "Local folder", local_box)
        row += 1

        self.f_mode = Gtk.ComboBoxText()
        for key, label in cfgmod.MODES:
            self.f_mode.append(key, label)
        self.f_mode.connect("changed", self._on_mode_changed)
        self._row(grid, row, "Mode", self.f_mode)
        row += 1

        self.mode_warning = _label("", wrap=True)
        grid.attach(self.mode_warning, 1, row, 1, 1)
        row += 1

        self.f_interval = Gtk.SpinButton.new_with_range(1, 1440, 1)
        self._row(grid, row, "Check every (min)", self.f_interval)
        row += 1

        self.f_watch = Gtk.CheckButton(label="Sync soon after a local change")
        grid.attach(self.f_watch, 1, row, 1, 1)
        row += 1

        self.f_debounce = Gtk.SpinButton.new_with_range(2, 600, 1)
        self._row(grid, row, "Wait after change (s)", self.f_debounce)
        row += 1

        self.advanced = Gtk.Expander(label="Advanced")
        box.pack_start(self.advanced, False, False, 0)
        adv = Gtk.Grid(column_spacing=10, row_spacing=8, margin_top=8)
        self.advanced.add(adv)
        arow = 0

        self.f_conflict = Gtk.ComboBoxText()
        for key, label in cfgmod.CONFLICT_CHOICES:
            self.f_conflict.append(key, label)
        self._row(adv, arow, "On conflict", self.f_conflict)
        arow += 1

        self.f_bwlimit = Gtk.Entry(placeholder_text="e.g. 2M or 500k — empty = unlimited")
        self._row(adv, arow, "Bandwidth limit", self.f_bwlimit)
        arow += 1

        self.f_transfers = Gtk.SpinButton.new_with_range(1, 32, 1)
        self._row(adv, arow, "Parallel transfers", self.f_transfers)
        arow += 1

        self.f_checkers = Gtk.SpinButton.new_with_range(1, 64, 1)
        self._row(adv, arow, "Parallel checks", self.f_checkers)
        arow += 1

        adv.attach(_label("Safety net", bold=True), 0, arow, 2, 1)
        arow += 1

        self.f_safety_backup = Gtk.CheckButton(
            label="Keep deleted and replaced files in a trash folder"
        )
        self.f_safety_backup.set_tooltip_text(
            "Instead of destroying a file, rclone moves it to a dated trash folder on "
            "both sides, so anything removed by mistake can be recovered."
        )
        adv.attach(self.f_safety_backup, 1, arow, 1, 1)
        arow += 1

        self.f_confirm_dirs = Gtk.CheckButton(
            label="Always ask before a deleted folder is removed on the other side"
        )
        self.f_confirm_dirs.set_tooltip_text(
            "Deleting single files syncs straight through. Deleting a whole folder asks "
            "first, so you can choose between deleting it everywhere, keeping it on the "
            "remote and unsyncing it, or restoring it."
        )
        adv.attach(self.f_confirm_dirs, 1, arow, 1, 1)
        arow += 1

        self.f_max_delete = Gtk.SpinButton.new_with_range(1, 100, 5)
        self._row(
            adv,
            arow,
            "Abort if deleting > %",
            self.f_max_delete,
            "If a sync would delete more than this share of the files, the whole run is "
            "cancelled and nothing is deleted.",
        )
        arow += 1

        self.f_trash_days = Gtk.SpinButton.new_with_range(0, 365, 1)
        self._row(adv, arow, "Keep trash for (days)", self.f_trash_days, "0 = keep forever")
        arow += 1

        trash_btn = Gtk.Button(label="Open local trash folder")
        trash_btn.connect("clicked", lambda *_a: util.open_path(util.TRASH_DIR))
        adv.attach(trash_btn, 1, arow, 1, 1)
        arow += 1

        self.f_skip_gdocs = Gtk.CheckButton(
            label="Skip Google Docs/Sheets/Slides (they are not real files)"
        )
        adv.attach(self.f_skip_gdocs, 1, arow, 1, 1)
        arow += 1

        self.f_mount_opts = Gtk.Entry()
        self.mount_row_widgets = (
            self._row(adv, arow, "Mount options", self.f_mount_opts),
            adv.get_child_at(0, arow),
        )
        arow += 1

        self.f_extra = Gtk.Entry(placeholder_text="extra rclone flags")
        self._row(adv, arow, "Extra flags", self.f_extra)
        arow += 1

        adv.attach(_label("Exclude patterns (one per line)"), 0, arow, 2, 1)
        arow += 1
        self.f_excludes = Gtk.TextView()
        self.f_excludes.set_monospace(True)
        exc_scroll = Gtk.ScrolledWindow()
        exc_scroll.set_shadow_type(Gtk.ShadowType.IN)
        exc_scroll.set_size_request(-1, 130)
        exc_scroll.add(self.f_excludes)
        adv.attach(exc_scroll, 0, arow, 2, 1)

        actions = Gtk.Box(spacing=6, margin_top=6)
        for label, handler, style in (
            ("Apply", self._on_apply, "suggested-action"),
            ("Preview (dry run)", self._on_dry_run, None),
            ("Sync now", self._on_sync_now, None),
            ("Run first sync…", self._on_resync, None),
            ("Open local folder", self._on_open_local, None),
            ("View log", self._on_view_log, None),
            ("Conflicts…", self._on_conflicts, None),
            ("Review deletion…", self._on_blocked, None),
        ):
            btn = Gtk.Button(label=label)
            if style:
                btn.get_style_context().add_class(style)
            btn.connect("clicked", handler)
            actions.pack_start(btn, False, False, 0)
            if label == "Conflicts…":
                self.conflicts_btn = btn
                btn.set_sensitive(False)
            if label == "Review deletion…":
                self.blocked_btn = btn
                btn.set_sensitive(False)
        box.pack_start(actions, False, False, 0)

        self.job_form_widgets = box
        return box

    def _on_mode_changed(self, _combo):
        mode = self.f_mode.get_active_id() or "bisync"
        warning = cfgmod.MODE_WARNINGS.get(mode, "")
        if warning:
            colour = "#c0392b" if mode in cfgmod.DESTRUCTIVE_MODES else "#8a6d1b"
            self.mode_warning.set_markup(
                "<span foreground='%s'>⚠ %s</span>"
                % (colour, GLib.markup_escape_text(warning))
            )
        else:
            self.mode_warning.set_text("")
        is_mount = mode == "mount"
        for widget in self.mount_row_widgets:
            if widget:
                widget.set_visible(is_mount)
                widget.set_no_show_all(not is_mount)
        self.f_conflict.set_sensitive(mode == "bisync")
        self.f_watch.set_sensitive(not is_mount)
        self.f_interval.set_sensitive(not is_mount)

    def refresh_jobs(self):
        selected = self._current_job_id
        self.job_store.clear()
        for job in self.config.jobs:
            runner = self.engine.runner(job["id"])
            state = runner.state if runner else eng.IDLE
            self.job_store.append(
                [job["id"], job.get("name") or "(unnamed)", STATE_ICONS.get(state, "folder")]
            )
        if not self.config.jobs:
            self.job_form_widgets.set_sensitive(False)
            self.job_status.set_text(
                "No synced folders yet. Press + to add one after connecting an account."
            )
            self._current_job_id = None
            return
        self.job_form_widgets.set_sensitive(True)
        target = selected or self.config.jobs[0]["id"]
        for row in self.job_store:
            if row[0] == target:
                self.job_view.get_selection().select_iter(row.iter)
                return
        self.job_view.get_selection().select_path(0)

    def _refresh_remote_combo(self):
        if not hasattr(self, "f_remote"):
            return
        current = self.f_remote.get_active_id()
        self.f_remote.remove_all()
        for name, rtype in rclone.listremotes():
            self.f_remote.append(name, "%s (%s)" % (name, rtype))
        if current:
            self.f_remote.set_active_id(current)

    def _on_job_selected(self, selection):
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
        job_id = model[treeiter][0]
        if job_id == self._current_job_id:
            return
        if self._current_job_id:
            self._save_current_job(reload_engine=True)
        self._current_job_id = job_id
        self._load_job(job_id)

    def _load_job(self, job_id):
        job = self.config.job(job_id)
        if not job:
            return
        self._loading = True
        self.f_name.set_text(job.get("name", ""))
        self.f_enabled.set_active(job.get("enabled", True))
        self._refresh_remote_combo()
        if job.get("remote"):
            self.f_remote.set_active_id(job["remote"])
        self.f_remote_path.set_text(job.get("remote_path", ""))
        self.f_local.set_text(job.get("local_path", ""))
        self.f_mode.set_active_id(job.get("mode", "bisync"))
        self.f_interval.set_value(job.get("interval_minutes", 5))
        self.f_watch.set_active(job.get("watch", True))
        self.f_debounce.set_value(job.get("watch_debounce_seconds", 15))
        self.f_conflict.set_active_id(job.get("conflict_resolve", "newer"))
        self.f_bwlimit.set_text(job.get("bandwidth_limit", ""))
        self.f_transfers.set_value(job.get("transfers", 4))
        self.f_checkers.set_value(job.get("checkers", 8))
        self.f_skip_gdocs.set_active(job.get("skip_gdocs", True))
        self.f_safety_backup.set_active(job.get("safety_backup", True))
        self.f_max_delete.set_value(job.get("max_delete_percent", 25))
        self.f_confirm_dirs.set_active(job.get("confirm_folder_deletions", True))
        self.f_trash_days.set_value(job.get("trash_days", 30))
        self.f_mount_opts.set_text(job.get("mount_options", ""))
        self.f_extra.set_text(job.get("extra_args", ""))
        self.f_excludes.get_buffer().set_text("\n".join(job.get("excludes") or []))
        self._on_mode_changed(None)
        self._loading = False
        self.update_job_status()

    def _collect_job(self, job):
        buf = self.f_excludes.get_buffer()
        excludes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        job.update(
            {
                "name": self.f_name.get_text().strip() or "Sync",
                "enabled": self.f_enabled.get_active(),
                "remote": self.f_remote.get_active_id() or "",
                "remote_path": self.f_remote_path.get_text().strip().strip("/"),
                "local_path": self.f_local.get_text().strip(),
                "mode": self.f_mode.get_active_id() or "bisync",
                "interval_minutes": int(self.f_interval.get_value()),
                "watch": self.f_watch.get_active(),
                "watch_debounce_seconds": int(self.f_debounce.get_value()),
                "conflict_resolve": self.f_conflict.get_active_id() or "newer",
                "bandwidth_limit": self.f_bwlimit.get_text().strip(),
                "transfers": int(self.f_transfers.get_value()),
                "checkers": int(self.f_checkers.get_value()),
                "skip_gdocs": self.f_skip_gdocs.get_active(),
                "safety_backup": self.f_safety_backup.get_active(),
                "max_delete_percent": int(self.f_max_delete.get_value()),
                "confirm_folder_deletions": self.f_confirm_dirs.get_active(),
                "trash_days": int(self.f_trash_days.get_value()),
                "mount_options": self.f_mount_opts.get_text().strip(),
                "extra_args": self.f_extra.get_text().strip(),
                "excludes": [ln.strip() for ln in excludes.splitlines() if ln.strip()],
            }
        )
        return job

    def _save_current_job(self, reload_engine=True):
        if self._loading or not self._current_job_id:
            return
        job = self.config.job(self._current_job_id)
        if not job:
            return
        self._collect_job(job)
        self.config.save()
        if reload_engine:
            self.engine.reload()
        self.refresh_jobs()
        self.app.update_ui()

    def _on_apply(self, _btn):
        self._save_current_job()
        self.update_job_status()

    def _on_add_job(self, _btn):
        remotes = rclone.listremotes()
        if not remotes:
            _message(
                self,
                Gtk.MessageType.WARNING,
                "Connect an account first",
                "Go to the Accounts tab and connect Google Drive.",
            )
            self.notebook.set_current_page(0)
            return
        if self._current_job_id:
            self._save_current_job(reload_engine=False)
        default_local = os.path.expanduser("~/Google Drive")
        index = 2
        taken = {j.get("local_path") for j in self.config.jobs}
        while default_local in taken:
            default_local = os.path.expanduser("~/Google Drive %d" % index)
            index += 1
        job = cfgmod.new_job(
            name=remotes[0][0],
            remote=remotes[0][0],
            local_path=default_local,
        )
        self.config.add_job(job)
        self.config.save()
        self.engine.reload()
        self._current_job_id = job["id"]
        self.refresh_jobs()
        self._load_job(job["id"])
        self.app.update_ui()

    def add_job_for_path(self, local_path):
        """Create (or focus) a sync pair for a folder chosen in the file manager."""
        for job in self.config.jobs:
            if os.path.abspath(os.path.expanduser(job.get("local_path") or "")) == os.path.abspath(
                local_path
            ):
                self._current_job_id = job["id"]
                self.refresh_jobs()
                self.present()
                return
        remotes = rclone.listremotes()
        if not remotes:
            self.notebook.set_current_page(0)
            return
        if self._current_job_id:
            self._save_current_job(reload_engine=False)
        job = cfgmod.new_job(
            name=os.path.basename(local_path.rstrip("/")) or remotes[0][0],
            remote=remotes[0][0],
            remote_path=os.path.basename(local_path.rstrip("/")),
            local_path=local_path,
            enabled=False,  # nothing happens until the user reviews and enables it
        )
        self.config.add_job(job)
        self.config.save()
        self.engine.reload()
        self._current_job_id = job["id"]
        self.refresh_jobs()
        self._load_job(job["id"])
        self.app.update_ui()
        self.notebook.set_current_page(1)
        self.present()
        _message(
            self,
            Gtk.MessageType.INFO,
            "Ready to sync “%s”" % os.path.basename(local_path.rstrip("/")),
            "Check the account and the folder in Drive, tick “Keep this folder in sync”, "
            "then press Apply. Nothing is transferred until you do.",
        )

    def _on_remove_job(self, _btn):
        if not self._current_job_id:
            return
        job = self.config.job(self._current_job_id)
        if not job:
            return
        if not _confirm(
            self,
            "Stop syncing “%s”?" % job.get("name"),
            "The pair is removed from this app. Nothing is deleted, locally or on Drive.",
        ):
            return
        self.config.remove_job(self._current_job_id)
        self.config.save()
        self._current_job_id = None
        self.engine.reload()
        self.refresh_jobs()
        self.app.update_ui()

    def _on_browse_remote(self, _btn):
        remote = self.f_remote.get_active_id()
        if not remote:
            _message(self, Gtk.MessageType.WARNING, "Choose an account first")
            return
        dialog = RemoteBrowser(self, remote, self.f_remote_path.get_text())
        if dialog.run() == Gtk.ResponseType.OK:
            self.f_remote_path.set_text(dialog.selected_path())
        dialog.destroy()

    def _on_browse_local(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Choose the local folder",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK
        )
        dialog.set_create_folders(True)
        current = self.f_local.get_text().strip()
        if current and os.path.isdir(current):
            dialog.set_current_folder(current)
        else:
            dialog.set_current_folder(os.path.expanduser("~"))
        if dialog.run() == Gtk.ResponseType.OK:
            self.f_local.set_text(dialog.get_filename())
        dialog.destroy()

    def _on_dry_run(self, _btn):
        self._save_current_job()
        runner = self.engine.runner(self._current_job_id)
        if not runner:
            return
        if runner.mode == "mount":
            _message(self, Gtk.MessageType.INFO, "Mount mode has nothing to preview")
            return
        DryRunDialog(self, runner)

    def _on_sync_now(self, _btn):
        self._save_current_job()
        runner = self.engine.runner(self._current_job_id)
        if not runner:
            return
        if runner.mode in cfgmod.DESTRUCTIVE_MODES and not runner.last_sync_ts:
            if not _confirm(
                self,
                "Run “%s” as a mirror for the first time?" % runner.name,
                "%s\n\nUse “Preview (dry run)” first if you are not sure."
                % cfgmod.MODE_WARNINGS.get(runner.mode, ""),
            ):
                return
        runner.request_sync("manual")

    def _on_resync(self, _btn):
        self._save_current_job()
        runner = self.engine.runner(self._current_job_id)
        if not runner:
            return
        if runner.mode != "bisync":
            runner.request_sync("manual")
            return
        if not _confirm(
            self,
            "Run the first two-way sync?",
            "rclone builds its baseline by merging both sides: files present only on one side "
            "are copied to the other, and for files that exist on both the newer one wins.\n\n"
            "Do this once per pair, or after a sync error asks for it.",
        ):
            return
        runner.resync_done = False
        runner.request_sync("resync", resync=True)

    def _on_open_local(self, _btn):
        job = self.config.job(self._current_job_id)
        if job and job.get("local_path"):
            path = os.path.expanduser(job["local_path"])
            os.makedirs(path, exist_ok=True)
            util.open_path(path)

    def _on_blocked(self, _btn):
        runner = self.engine.runner(self._current_job_id)
        if runner and runner.safety_blocked:
            BlockedDeletionsDialog(self, runner)

    def _on_conflicts(self, _btn):
        runner = self.engine.runner(self._current_job_id)
        if runner and runner.conflicts:
            ConflictsDialog(self, runner)

    def _on_view_log(self, _btn):
        if self._current_job_id:
            self.notebook.set_current_page(2)
            self._select_log(self._current_job_id)

    def update_job_status(self):
        runner = self.engine.runner(self._current_job_id) if self._current_job_id else None
        if not runner:
            self.job_status.set_text("")
            return
        bits = [runner.status_text()]
        if runner.quota:
            used = util.human_size(runner.quota.get("used"))
            total = runner.quota.get("total")
            bits.append("Drive: %s%s" % (used, " of %s" % util.human_size(total) if total else ""))
        if runner.conflicts:
            bits.append("%d conflict(s) to review" % len(runner.conflicts))
        if runner.last_result:
            bits.append(runner.last_result)
        self.job_status.set_text(" · ".join(b for b in bits if b))
        self.conflicts_btn.set_sensitive(bool(runner.conflicts))
        self.blocked_btn.set_sensitive(bool(runner.safety_blocked))

    # ------------------------------------------------------------ logs tab

    def _build_logs(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)

        bar = Gtk.Box(spacing=6)
        self.log_combo = Gtk.ComboBoxText()
        self.log_combo.connect("changed", lambda *_a: self._load_log())
        bar.pack_start(self.log_combo, True, True, 0)
        self.log_follow = Gtk.CheckButton(label="Follow")
        self.log_follow.set_active(True)
        bar.pack_start(self.log_follow, False, False, 0)
        for label, handler in (
            ("Reload", lambda *_a: self._load_log()),
            ("Open in editor", self._on_open_log_external),
            ("Clear", self._on_clear_log),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            bar.pack_start(btn, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.log_view = Gtk.TextView(editable=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.override_font(Pango.FontDescription("monospace 9"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.log_view)
        self.log_scroller = scroller
        box.pack_start(scroller, True, True, 0)

        self.log_path_label = _label("", dim=True)
        box.pack_start(self.log_path_label, False, False, 0)
        return box

    def _refresh_log_combo(self):
        current = self.log_combo.get_active_id()
        self.log_combo.remove_all()
        for job in self.config.jobs:
            self.log_combo.append(job["id"], "%s — sync log" % job.get("name"))
        self.log_combo.append("__app__", "Application log")
        if current:
            self.log_combo.set_active_id(current)
        elif self.config.jobs:
            self.log_combo.set_active_id(self.config.jobs[0]["id"])
        else:
            self.log_combo.set_active_id("__app__")

    def _select_log(self, job_id):
        self._refresh_log_combo()
        self.log_combo.set_active_id(job_id)

    def _current_log_path(self):
        key = self.log_combo.get_active_id()
        if not key:
            return None
        if key == "__app__":
            return os.path.join(util.LOG_DIR, "app.log")
        return util.job_log_path(key)

    def _load_log(self):
        path = self._current_log_path()
        buf = self.log_view.get_buffer()
        if not path or not os.path.exists(path):
            buf.set_text("No log yet.")
            self.log_path_label.set_text(path or "")
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                text = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            text = str(exc)
        buf.set_text(text)
        self.log_path_label.set_text("%s — %s" % (path, util.human_size(os.path.getsize(path))))
        if self.log_follow.get_active():
            GLib.idle_add(self._scroll_log_end)

    def _scroll_log_end(self):
        buf = self.log_view.get_buffer()
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        buf.delete_mark(mark)
        return GLib.SOURCE_REMOVE

    def _on_open_log_external(self, _btn):
        path = self._current_log_path()
        if path and os.path.exists(path):
            util.open_path(path)

    def _on_clear_log(self, _btn):
        path = self._current_log_path()
        if path and os.path.exists(path) and _confirm(self, "Clear this log?", path):
            try:
                open(path, "w").close()
            except OSError:
                pass
            self._load_log()

    def _on_switch_page(self, _nb, _page, index):
        if index == 2:
            self._refresh_log_combo()
            self._load_log()
            if not self._log_timer:
                self._log_timer = GLib.timeout_add_seconds(3, self._tick_log)
        elif self._log_timer:
            GLib.source_remove(self._log_timer)
            self._log_timer = None
        if index == 0:
            self.refresh_rclone_status()
            self.refresh_security()

    def _tick_log(self):
        if self.log_follow.get_active():
            self._load_log()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ general tab

    def _build_general(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(16)

        box.pack_start(_label("Startup", bold=True), False, False, 0)
        self.g_autostart = Gtk.CheckButton(label="Start automatically when I log in")
        self.g_autostart.set_active(util.autostart_enabled())
        self.g_autostart.connect(
            "toggled", lambda w: util.set_autostart(w.get_active())
        )
        box.pack_start(self.g_autostart, False, False, 0)

        self.g_sync_start = Gtk.CheckButton(label="Sync once right after starting")
        self.g_sync_start.set_active(self.config.get("sync_on_start", True))
        self.g_sync_start.connect("toggled", self._on_general_toggle, "sync_on_start")
        box.pack_start(self.g_sync_start, False, False, 0)

        box.pack_start(_label("Notifications", bold=True), False, False, 0)
        self.g_notify = Gtk.CheckButton(label="Show desktop notifications on problems")
        self.g_notify.set_active(self.config.get("notifications", True))
        self.g_notify.connect("toggled", self._on_general_toggle, "notifications")
        box.pack_start(self.g_notify, False, False, 0)

        self.g_notify_ok = Gtk.CheckButton(label="Also notify after every successful sync")
        self.g_notify_ok.set_active(self.config.get("notify_on_success", False))
        self.g_notify_ok.connect("toggled", self._on_general_toggle, "notify_on_success")
        box.pack_start(self.g_notify_ok, False, False, 0)

        box.pack_start(_label("Network", bold=True), False, False, 0)
        self.g_metered = Gtk.CheckButton(label="Sync on metered connections (mobile tethering)")
        self.g_metered.set_active(self.config.get("sync_on_metered", True))
        self.g_metered.connect("toggled", self._on_general_toggle, "sync_on_metered")
        box.pack_start(self.g_metered, False, False, 0)

        box.pack_start(_label("Sign-in browser", bold=True), False, False, 0)
        box.pack_start(
            _label(
                "Signing in to Google happens in a browser. Pick the one where your "
                "Google account is already logged in — it does not have to be the system "
                "default.",
                dim=True,
                wrap=True,
            ),
            False,
            False,
            0,
        )
        browser_row = Gtk.Box(spacing=8)
        self.g_browser = Gtk.ComboBoxText()
        self.g_browser.append("", "System default browser")
        for command, label in util.detected_browsers():
            self.g_browser.append(command, label)
        current = self.config.get("auth_browser", "") or ""
        if current and not any(
            current == command for command, _l in util.detected_browsers()
        ):
            self.g_browser.append(current, "%s (custom)" % current)
        self.g_browser.set_active_id(current)
        self.g_browser.connect("changed", self._on_browser_changed)
        browser_row.pack_start(self.g_browser, False, False, 0)
        test_btn = Gtk.Button(label="Test")
        test_btn.set_tooltip_text("Open a Google page with the selected browser")
        test_btn.connect(
            "clicked", lambda *_a: util.open_url("https://myaccount.google.com/permissions")
        )
        browser_row.pack_start(test_btn, False, False, 0)
        box.pack_start(browser_row, False, False, 0)

        box.pack_start(_label("About", bold=True), False, False, 0)
        parts, raw = rclone.version()
        info = [
            "%s %s" % (APP_NAME, __version__),
            "Unofficial Google Drive client — not affiliated with Google LLC.",
            "rclone: %s" % (raw or "not installed"),
            "Tray backend: %s" % self.app.tray.describe(),
            "Desktop: %s" % (util.desktop_name() or "unknown"),
            "Config: %s" % util.CONFIG_FILE,
            "rclone config: %s%s"
            % (rclone.config_path(), " (encrypted)" if rclone.config_is_encrypted() else ""),
            "Logs: %s" % util.LOG_DIR,
        ]
        box.pack_start(_label("\n".join(info), dim=True), False, False, 0)

        links = Gtk.Box(spacing=6)
        for label, path in (
            ("Open config folder", util.CONFIG_DIR),
            ("Open log folder", util.LOG_DIR),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, p=path: util.open_path(p))
            links.pack_start(btn, False, False, 0)
        project = Gtk.LinkButton.new_with_label(PROJECT_URL, "Project page on GitHub")
        project.connect("activate-link", lambda *_a: (util.open_url(PROJECT_URL), True)[1])
        links.pack_start(project, False, False, 0)
        issues = Gtk.LinkButton.new_with_label(ISSUES_URL, "Report an issue")
        issues.connect("activate-link", lambda *_a: (util.open_url(ISSUES_URL), True)[1])
        links.pack_start(issues, False, False, 0)
        box.pack_start(links, False, False, 0)

        box.pack_start(
            _label(
                "Free software under the MIT licence, by Jose Roig Borrell (github.com/rrroig).\n"
                "Unofficial client: not affiliated with, endorsed by, or connected to Google LLC.",
                dim=True,
                wrap=True,
            ),
            False,
            False,
            0,
        )
        return box

    def _on_browser_changed(self, combo):
        command = combo.get_active_id() or ""
        self.config.set("auth_browser", command)
        self.config.save()
        util.set_web_browser(command)

    def _on_general_toggle(self, widget, key):
        self.config.set(key, widget.get_active())
        self.config.save()

    # ------------------------------------------------------------ misc

    def on_engine_change(self):
        if self.get_visible():
            self.refresh_jobs()
            self.update_job_status()

    def _on_close(self, *_args):
        self._save_current_job()
        if self._log_timer:
            GLib.source_remove(self._log_timer)
            self._log_timer = None
        self.hide()
        return True
