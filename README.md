# OSO Rclone Desktop

A Dropbox-style tray application for [rclone](https://rclone.org). Point it at a
Google Drive account and a local folder, and it keeps both sides in sync in the
background — with a status icon, desktop notifications, and a settings window for
accounts, folders and logs.

Built for **GNOME** and **Cinnamon** (Ubuntu 22.04+ and Linux Mint 22.x), it also
runs on MATE, Xfce, Budgie and KDE.

![status: idle / syncing / error](oso_rclone_desktop/icons/oso-rclone-desktop-idle.svg)

## What it does

- **Two-way sync** of a local folder with Google Drive (`rclone bisync`) — edit
  files anywhere, they converge. One-way and mount modes are available too.
- **Reacts to your edits.** A recursive file-system watch triggers a sync a few
  seconds after you touch a file, so you do not wait for the next interval.
- **Tray icon** showing idle / syncing / error / paused at a glance, with per-folder
  status, "Sync now", quota, and quick links to the folder and the log.
- **Settings window** with four tabs: Accounts, Synced folders, Logs, General.
- **Multiple folders and multiple accounts** — one entry per pair.
- **Safety net**: aborts a run that would delete too much, keeps deleted files in a
  dated trash on both sides, and offers a dry-run preview before touching anything.
- **Survives reality**: offline detection, metered-connection handling, retries,
  first-run baseline handling, log rotation, single instance.

## Install

```bash
git clone https://github.com/rrroig/oso-rclone-desktop.git
cd oso-rclone-desktop
./install.sh              # per user, into ~/.local — no root needed for the app itself
```

The installer checks your desktop, installs the GTK/tray packages it needs, offers
to install a current rclone, and on GNOME offers to enable the AppIndicator
extension. Other options:

```bash
./install.sh --system      # install into /usr/local for all users
./install.sh --autostart   # also start it at login
./install.sh --no-deps     # skip apt and rclone handling
./uninstall.sh [--purge]
```

Then launch **OSO Rclone Desktop** from your applications menu. The first run opens
the settings window.

### Requirements

| | |
|---|---|
| rclone | **1.66 or newer** for two-way sync. The version in Ubuntu 22.04 (1.53) is too old; the installer offers the official build. |
| Python | 3.8+ with PyGObject (`python3-gi`) |
| Tray | GNOME needs the AppIndicator extension; Cinnamon/Mint works out of the box |

### Tray backends

The status icon is drawn through whichever mechanism your desktop actually
supports, picked automatically:

| Desktop | Backend |
|---|---|
| Cinnamon (Mint), MATE, Xfce, Budgie | `XApp.StatusIcon` — native, real tooltips |
| GNOME, KDE, Unity | `AppIndicator` / StatusNotifierItem |
| anything else / fallback | `Gtk.StatusIcon` (legacy XEmbed tray) |

Force one with `OSO_TRAY_BACKEND=xapp|appindicator|gtk`.

On **GNOME**, tray icons only appear if the AppIndicator extension is installed and
enabled (`gnome-shell-extension-appindicator`); log out and back in after
installing it. On **Cinnamon** nothing extra is needed.

## First run

1. **Accounts → Connect Google Drive…** — a terminal opens, your browser asks you
   to sign in, and rclone stores the resulting token. Nothing else to configure.
2. **Synced folders → +** — pick the account, the folder inside Drive (there is a
   remote folder browser), and the local folder.
3. **Run first sync…** — for two-way mode, rclone needs one baseline pass. It
   merges both sides: files that exist on only one side are copied to the other,
   and for files on both sides the newer one wins. After that, syncs are
   incremental.

## Sync modes

| Mode | What happens |
|---|---|
| **Two-way sync** (default) | Changes flow both ways, deletions included. Dropbox-like. |
| Upload only | Local → Drive, never deletes on Drive. Safe backup. |
| Mirror local → Drive | Drive becomes an exact copy of the local folder, deletions included. |
| Download only | Drive → local, never deletes locally. |
| Mirror Drive → local | Local becomes an exact copy of Drive, deletions included. |
| Mount | Drive appears as a folder without a local copy (`rclone mount`, needs fuse3). |

## Safety: it will not wipe your Drive

Two-way sync propagates deletions by design, so the app ships with three guards, all
visible under **Synced folders → Advanced → Safety net**:

- **Delete guard.** If a single run would delete more than a share of the files
  (**25 % by default**), rclone aborts the whole run and deletes *nothing*. The tray
  reports "Stopped on purpose", so a wiped local folder, an unmounted disk or a wrong
  path cannot cascade to Drive.
- **Trash instead of destruction.** Deleted and overwritten files are *moved* to a dated
  trash folder rather than removed: `.oso-trash/<folder>/<date>/` inside your Drive, and
  `~/.local/share/oso-rclone-desktop/trash/` locally. Kept for 30 days by default.
  (The remote trash needs the synced folder to be a subfolder of the Drive, not its root.)
- **Preview (dry run).** A button that runs the real command with `--dry-run` and shows
  exactly what would be copied, moved and deleted, changing nothing. Worth using before
  the first sync of any pair.

- **Unmounted-disk guard.** If the local folder is suddenly empty but held files at the
  last sync — an external disk not mounted, a network share gone, a wrong path — the sync
  is refused before rclone even starts, so the emptiness cannot propagate to Drive.
- **Google Drive's own bin.** rclone deletes through Drive's trash by default, so removed
  files also stay recoverable at drive.google.com for 30 days. That is a third net,
  independent of this app.

Beyond that: mirror modes carry a red warning in the UI and ask for confirmation before
their first run, and if rclone loses its baseline it stops and asks rather than guessing.

### Deleting a folder always asks

Deleting single files syncs straight through — that is the point of a sync tool. Deleting
a whole **folder** is different: on disk it is indistinguishable from "I no longer want
this folder synced", and the two mean opposite things. So a folder that disappears
locally always stops the sync and asks, however small it is:

- **Delete them everywhere** — apply the deletion to the remote, via the trash folder.
- **Keep them, stop syncing** — the folder stays untouched on the remote and is added to
  this pair's exclude rules. Nothing is deleted anywhere.
- **Restore them here** — you deleted it by mistake; download it back from the remote.

Turn this off per folder with *Always ask before a deleted folder is removed* under
Advanced → Safety net.

### When the delete guard trips

Independently of folders, any run that would delete more than the allowed share of the
files stops and shows the exact list, so you can approve it or walk away. A deliberate
mass deletion costs you one confirmation; an accidental one costs you nothing. Raise
*Abort if deleting > %* if the prompt feels too eager, or set it to 100 to rely on the
trash alone.

### Sync a subfolder, not your whole Drive

You rarely want the entire Drive. In **Synced folders**, set *Folder in Drive* to a
subfolder — use the **Browse…** button to pick or create one, for example `Work`. Only
that subtree is synced; everything else in your Drive is never read, moved or deleted.
This is also what makes the remote trash possible.

## Conflicts

A conflict is when the *same file* changed on *both* sides between two syncs.
Nothing is ever silently discarded:

- The winner (by default the **newer** file — configurable to older/larger/smaller,
  or "keep both") keeps its original name.
- The loser is renamed to `name.conflict1` and copied to **both** sides, next to
  the winner, so you can compare and merge.
- The tray menu and the settings window then show **"Review N conflict(s)…"**,
  which lists the files and opens the folder. "Mark as reviewed" clears the badge.

To reduce conflicts: keep the sync interval short, leave "sync soon after a local
change" on, and avoid editing the same file on two machines at once. Office-style
lock files (`.~lock.*`, `~$*`) are excluded by default.

If rclone ever loses its baseline (interrupted run, moved folder) it refuses to
guess and reports **"First sync required"** rather than deleting anything; run
*Run first sync…* to rebuild it.

## Security of the Google sign-in

- Sign-in uses **OAuth in your browser**. The app never sees, asks for, or stores
  your Google password.
- rclone stores the resulting **token** in `~/.config/rclone/rclone.conf`. Anyone
  who can read that file can access those accounts, so:
  - **Accounts → Fix file permissions** sets it to `0600` (owner only).
  - **Accounts → Set config password…** turns on rclone's own config encryption.
    The file then becomes unreadable without the password, and this app asks for it
    once per session and keeps it in memory only (`RCLONE_CONFIG_PASS`), never on
    disk.
- The Accounts tab always shows the current state: file path, encryption on/off,
  and whether the permissions are loose.
- To revoke access entirely, remove the remote here and revoke the app at
  [Google account permissions](https://myaccount.google.com/permissions).

## Where things live

| | |
|---|---|
| Settings | `~/.config/oso-rclone-desktop/config.json` |
| Sync state | `~/.local/state/oso-rclone-desktop/state.json` |
| Logs | `~/.local/state/oso-rclone-desktop/logs/` (rotated at 5 MB) |
| Local trash | `~/.local/share/oso-rclone-desktop/trash/` |
| Drive trash | `.oso-trash/` in the account, next to the synced folder |
| Account tokens | `~/.config/rclone/rclone.conf` (managed by rclone) |

## Command line

```bash
oso-rclone-desktop              # start the tray app
oso-rclone-desktop --settings   # start with the settings window open
oso-rclone-desktop --sync       # sync every folder once and exit (cron/systemd)
oso-rclone-desktop --version
```

## Building a .deb

```bash
./packaging/build-deb.sh        # produces dist/oso-rclone-desktop_<version>_all.deb
sudo apt install ./dist/oso-rclone-desktop_*.deb
```

## Licence

MIT — see [LICENSE](LICENSE).

rclone itself is a separate project (MIT licensed) and is not bundled here.
