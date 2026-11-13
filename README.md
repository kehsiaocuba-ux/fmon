# fmon
Directory state snapshot generator and recursive folder checker.

fmon is open source (MIT license), by Alejandro Castaneda. It runs on
Windows, Linux and macOS (Python 3.9+; the GUI needs Tk, see
`packaging/README.md`).

## Installation

    pip install .                       # 'fmon' and 'fmon-gui' commands (any platform)
    .\packaging\windows\build.ps1       # Windows: fmon.exe, fmon-gui.exe and an installer
                                        # (setup.exe with Inno Setup, else a portable ZIP + install.ps1)
    packaging/linux/                    # Linux: .desktop entry and a systemd unit for 'fmon watch'

## Getting help

    fmon                   # the complete manual, paged like `man` (also: fmon help)
    fmon help config       # the manual of one command, with examples
    fmon config -h         # short option summary of a command, with examples
    fmon -h                # command overview

## Single folder

Create a control file (snapshot) of a folder and all of its subfolders:

    fmon snapshot test1 -o test1.ctl
    fmon test1 -o test1.ctl          # shortcut
    fmon test1                       # default control file: test1.ctl (next to the folder)
    fmon test1 -n websrv             # record it under the target name "websrv"

Every control file fmon creates is recorded as a target in your config
file (created on first use if it does not exist), named after the folder
unless `-n NAME` is given:

    [test1]
    path = C:\Users\kehsi\fmon_workspace\test1
    control_file = C:\Users\kehsi\fmon_workspace\test1.ctl

Snapshotting the same folder again reuses the entry; a different control
file for the same folder updates it; a second folder with the same base
name gets a suffixed name (`test1-2`). Reusing a control file that is
already registered for another folder is refused.

Recursively check the folder against the control file. The folder and
the control file can be given in either order, and either can be left
out when the control file is named `<folder>.ctl`:

    fmon check test1 -c test1.ctl
    fmon test1 -c test1.ctl          # shortcut
    fmon -c test1.ctl                # folder inferred from the control file name
    fmon -c test1                    # control file inferred: test1.ctl
    fmon test1 -c                    # same

## Your personal config file

Every user has exactly one fmon config file:

* Windows: `%APPDATA%\fmon\fmon.ini`
* Linux/macOS: `~/.config/fmon/fmon.ini` (or `$XDG_CONFIG_HOME/fmon/fmon.ini`)

The location can be changed with `fmon config move PATH` or in the GUI
(Settings tab); it is remembered in `preferences.json` in the directory
above. The `FMON_CONFIG` environment variable overrides it for a shell.

    fmon config init       # create it from a template
    fmon config            # show the file, whether you can modify it, and its targets
    fmon config show test1 # show only target test1 (raw entry + resolved paths)
    fmon config show "t*"  # all targets whose name starts with t (glob patterns)

Edit it from the command line (the result is validated before anything is
written, so a conflicting or invalid change is refused and the file is
left untouched; comments in the file are preserved):

    fmon config add usr /usr                     # new target -> usr.ctl
    fmon config add home /home -c home-base.ctl  # with an explicit control file
    fmon config delete home                      # remove the entry, keep the control file
    fmon config delete "test*" --ctl             # remove entries AND delete their control files
    fmon config set control_dir /var/lib/fmon    # [fmon] settings by name ...
    fmon config set log_max_size 5M
    fmon config update log_keep_days 60          # 'update' is the same as 'set'
    fmon config set usr control_file usr-2026.ctl  # ... target options: TARGET OPTION VALUE
    fmon config unset usr control_file
    fmon config get                              # effective values of all [fmon] settings
    fmon config get log_file
    fmon config move D:\\fmon\\fmon.ini           # keep your config elsewhere (remembered in preferences.json)

`delete --ctl` edits the config first, so control files can only be
deleted by users permitted to modify the config - a clean-up job for
administrators.

Administrators can lock the configuration by making the file read-only
for users. Users can still view it and run `snapshot`/`check` on the
targets it defines, but any `add`/`delete`/`set`/`unset` is refused -
and so is snapshotting a new folder, since that would need a new entry:

    error: you are not permitted to modify /home/bob/.config/fmon/fmon.ini (read-only)
           ask an administrator to change the configuration

All `config` actions accept `-f INI` to operate on another file (e.g. a
system-wide `/etc/fmon.ini`); `fmon DIR -f INI` registers the new
control file there instead of in your personal config.

When `snapshot` or `check` is run without a directory, the targets from
this file are used:

    fmon snapshot              # snapshot every target in your config
    fmon check                 # check every target
    fmon check -t usr -t etc   # only the named targets

The same folder can be monitored by several users at once - each one
keeps their own control files, defined in their own config file.

## Config file format

Define every folder to protect as a named target, each with its own
control file (see `fmon.ini.example`):

    [fmon]
    control_dir = /var/lib/fmon      ; default location of control files

    [usr]
    path = /usr                      ; -> /var/lib/fmon/usr.ctl

    [home]
    path = /home
    control_file = home-baseline.ctl ; relative -> inside control_dir
    schedule = 15m                   ; checked every 15 min by the GUI / fmon watch

    [etc]
    path = /etc
    control_file = /root/fmon/etc.ctl ; absolute -> used as-is

Rules enforced when the config is loaded:

* Target names must be unique and may only contain letters, digits,
  `.`, `_` and `-` (they become file names).
* Every target needs a `path`.
* No two targets may share a control file (checked after resolving
  symlinks and, on Windows, case).
* `control_dir` and relative `path` values are resolved against the
  directory containing the `.ini` file; if `[fmon]`/`control_dir` is
  omitted, control files are written next to the `.ini`.
* A control file placed inside its own monitored folder is excluded from
  the scan so it never reports itself as added/modified.

* Missing `control_dir` directories are created by `snapshot`.

An explicit file can be used instead of your personal one with `-f`
(e.g. a system-wide `/etc/fmon.ini` run by root):

    fmon snapshot -f /etc/fmon.ini
    fmon check    -f /etc/fmon.ini -t usr -t etc

### Protecting the control files

The control files are the trusted baseline, so they should live where
the users being audited cannot modify them. For example, as root:

    mkdir -m 755 /var/lib/fmon
    fmon snapshot -f /etc/fmon.ini     # writes root-owned .ctl files

Regular users can neither replace the control files nor tamper with the
baseline, while root can run `fmon check -f /etc/fmon.ini` at any time
to get a report of what users changed.

## Scheduled monitoring

Give a target a `schedule` and it is checked at that interval while the
GUI is open or `fmon watch` runs:

    fmon config set website schedule 15m     # units: s, m, h, d; "off" disables
    fmon watch                               # headless; Ctrl+C stops
    fmon watch -i -v -f /etc/fmon.ini        # check now, show reports, system-wide config

Scheduled checks are ordinary `fmon check -t NAME` runs, so they appear
in the audit log with their full report, tagged `[scheduled]`. Schedule
changes are picked up automatically (the config is re-read every 10 s).

## Graphical application

    fmon gui                 # or: fmon-gui   (needs Tk, included with Python on Windows/macOS)
    fmon gui -f /etc/fmon.ini

Tabs:

* **Targets** - all targets with folder, control file status and schedule;
  filter by name/folder/glob; Snapshot, Check, Add, Edit, Delete (with
  optional deletion of control files).
* **Run** - type any fmon command (`-f <config>` is added automatically)
  or use the quick buttons; output goes to the console.
* **Schedule** - scheduled targets with last run, result, summary and a
  live countdown to the next run; set intervals, run now, start/stop the
  scheduler (it starts automatically if any target has a schedule).
* **Log** - the audit log in pages (`1 - 25 of 400`, 25/50/100 per page,
  first/prev/next/last, go to page), filters by type, user, command,
  free text (including the reports) and date range, optionally including
  rotated log files; the full entry is shown below the table.
* **Settings** (scrollable) - the config file in use (switch to another, create it),
  whether you may modify it, where your personal config lives (directory
  and file name - change it if you may write there; the file is moved and
  the choice remembered), the `[fmon]` settings with Browse buttons and
  their effective values, the appearance (color scheme incl. dark modes,
  font, size) and the raw file contents.
* **Help** - the full manual with section navigation and search; About
  (menu or button) shows version, license and author.

The divider between the tabs and the console can be dragged; its position
is remembered per tab (the console is hidden on the Log and Help tabs),
as are the window size, color scheme and font - in `preferences.json`
next to your default config.

If the config file cannot be loaded, the Targets and Settings tabs say
so in a banner (with the reason); obsolete options from earlier versions
are tolerated and can be removed with one click in Settings.

Every GUI action is executed through the command line (`python -m fmon
...`), so it is audit-logged like a terminal command, tagged `[gui]`,
and a read-only config is respected with a "not permitted" message.

## Audit log

Every command is recorded in a log file. Each entry starts with a line of
the form `TYPE | TIMESTAMP | USER | COMMAND | RESULT`; a check is followed
by its full report (indented), so the log shows exactly which files were
added, removed or modified, when, and by which user's command:

    Info    | 2026-11-06 23:02:02 | kehsi | fmon -c test2 | C:\...\test2: 1 added, 1 removed, 0 modified
        fmon check: C:\...\test2
        Control file: C:\...\test2.ctl
        Added: 1
          another.txt  (size 34 B, perms 0666 (rw-rw-rw-))
        Removed: 1
          old.txt  (size 3 B, perms 0666 (rw-rw-rw-))
        Modified: 0
    Warning | 2026-11-06 23:05:09 | kehsi | fmon config set fmon control_dir /x | you are not permitted to modify C:\...\fmon.ini (read-only)
    Error   | 2026-11-06 23:06:11 | kehsi | fmon -c nope.ctl | fmon: nope.ctl is neither a directory nor an existing control file

* `Info` - the command succeeded (a check that finds changes is a
  successful command; the changes are in the result and the report).
* `Warning` - the user attempted an action reserved to administrators,
  i.e. modifying a read-only configuration (including snapshotting a new
  folder, which would add an entry to it).
* `Error` - the command failed: permission errors writing control files,
  broken command lines, missing folders or control files, invalid config.

    fmon log             # show the last 20 entries
    fmon log -n 0        # show everything
    fmon log --files     # list the rotated log files

By default the log is `fmon.log` next to your config file. When it would
exceed 1 MB it is renamed after the period it covers, e.g.
`fmon_2026-11-06-21-00-05_to_2026-11-06-23-00-10.log` (first and last
entry). Rotated logs are kept forever unless `log_keep_days` is set, in
which case those older than that many days are deleted. Configure it in
`[fmon]`:

    [fmon]
    log_file = /var/log/fmon/fmon.log   ; "off" disables logging
    log_max_size = 5M                   ; bytes, or K/M/G suffix; 0 = never rotate
    log_keep_days = 60                  ; 0 = keep rotated logs forever

With `-f INI`, the log settings of that config file apply, so a
system-wide config can keep its own central log. A log that cannot be
written never breaks a command; a warning is printed instead.

## Report

The report is titled with the absolute path of the checked folder (and
the target name when using a config) and lists files that were added,
removed, or modified (size, permissions or mtime changed) anywhere in
the tree. Modified files show the before and after values of each
attribute, with changed ones marked by `*`:

    fmon check [usr]: /usr
    ======================
    Control file: /var/lib/fmon/usr.ctl

    Added: 1
      new/d.txt  (size 2 B, perms 0644 (rw-r--r--))
    Removed: 1
      a.txt  (size 2 B, perms 0644 (rw-r--r--))
    Modified: 1
      sub/deep/c.txt
          size:  2 B -> 16 B  *
          perms: 0644 (rw-r--r--) -> 0600 (rw-------)  *
          mtime: 2026-11-06 01:22:15 -> 2026-11-06 01:22:15  *

With several targets, one report is printed per target. Exit codes:

* `0` – no changes detected
* `1` – changes detected in at least one target
* `2` – error (bad config, missing folder or control file, ...); errors
  in one target do not stop the others from being checked
