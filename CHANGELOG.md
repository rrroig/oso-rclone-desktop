# Changelog

## 1.1.0 — 2026-08-17

First release used against a real Google Drive account, which is where most of
this came from.

### Safety

Two-way sync propagates deletions, so an accident on one side reaches the other.
Five layers now stand in the way, each verified by running rclone for real:

- **Deleting a folder always asks**, however small it is, because on disk
  "I deleted this folder" and "I no longer want it synced" are the same event
  and mean opposite things. The answer can be: delete it on the remote too,
  keep it there and stop syncing it, or restore it from the remote.
- **A run that would delete more than a set share of the files** (25% by
  default) is aborted whole, deleting nothing, and offered for approval with
  the exact list of what it wanted to remove.
- **A local folder that is suddenly empty** but held files at the last sync —
  an unmounted disk, a wrong path — refuses to sync at all.
- **Deleted and overwritten files are moved to a dated trash** on both sides
  rather than destroyed, kept for 30 days. That covers overwrites too, so it
  doubles as versioning.
- **Pairs pointed at an entire Drive** refuse to run without an explicit
  opt-in, and Google's own bin remains as a last resort.

### Choosing what to sync

- Authorising an account opens a **folder tree** that loads one level at a time,
  so folders nested at any depth can be picked. Each tick becomes its own pair,
  created disabled; anything unticked is never read.
- Files sitting loose at the top level of the account get their own option.
- Folders that already have a pair are shown as such, and duplicate pairs are
  rejected — two pairs over the same folders fought over rclone's lock and each
  one's watcher retriggered the other.
- **Preview (dry run)** shows exactly what a sync would copy, move and delete.

### Access and credentials

- The Connect dialog asks **how much access to grant**: full, only files the app
  creates, or read-only — and explains what each means.
- **Accounts → Restrict to folder…** pins an account to a single folder, after
  which rclone cannot see anything outside it. Google has no per-folder scope,
  so this is the only way to really narrow a grant.
- A four-step guide for creating **your own Google client ID**, including the
  two steps that fail silently otherwise: enabling the Drive API, and moving the
  consent screen to production so tokens stop expiring weekly. rclone's shared
  client ID is being retired during 2026.
- The credential JSON Google hands out can be **loaded directly**.
- The **sign-in browser is selectable**, for when the Google session lives in a
  different browser from the system default.
- The Accounts list shows what each account actually granted.

### Desktop integration

- A **Sync with Google Drive** entry in Nemo, Dolphin, Nautilus and Caja, each
  through its own native mechanism.
- Tray icon via XApp on Cinnamon, MATE and Xfce, AppIndicator on GNOME and KDE.

### Fixes

- The settings window could freeze itself: rebuilding the pair list moved the
  selection, which was read as the user changing pairs, which rebuilt the list.
  A **freeze watchdog** now dumps every thread's stack when the interface stalls,
  which is how this was found instead of guessed.
- Read-only rclone queries are cached and concurrent identical calls collapsed:
  a three-pair account was spawning a 70 MB binary four times at once and
  re-reading its configuration sixty times over.
- Previewing a brand new pair aborted because the local folder did not exist yet.
- Stopping a pair no longer blocks the interface waiting for rclone.

## 1.0.0 — 2026-08-16

Initial release: two-way sync with rclone bisync, tray status icon, settings
window with accounts, folders, live logs and general options, conflict review,
file-system watching, and an installer for Ubuntu and Linux Mint.
