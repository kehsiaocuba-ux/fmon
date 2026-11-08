"""The fmon manual: one source of documentation for

  * `fmon` / `fmon help`          - the full manual, paged like `man`
  * `fmon help COMMAND`           - one command
  * `fmon COMMAND -h`             - argparse help (description + examples)
"""
import os
import pydoc
import sys
import textwrap

from fmon import config as fmon_config

WIDTH = 78
INDENT = "    "

# ---------------------------------------------------------------------------
# Command reference. Each command: summary, usage lines, description,
# options [(flag, help)], examples [(command, explanation)].
# ---------------------------------------------------------------------------

COMMANDS = {
    "snapshot": {
        "summary": "Create control file(s) - the trusted baseline of one or more folders",
        "usage": [
            "fmon snapshot DIR [-o CONTROL_FILE] [-n NAME] [-f INI]",
            "fmon snapshot [-f INI] [-t NAME ...]",
            "fmon DIR [-o CONTROL_FILE]                  (shortcut)",
        ],
        "description": """
            Recursively scans a folder and all of its subfolders and writes a
            control file (.ctl) recording every file's size, permissions and
            modification time. The control file is the baseline that later
            'check' runs compare against.

            With a DIR, one control file is created (default: DIR.ctl next to
            the folder) and the folder is registered as a target in your
            config file - created if it does not exist yet - under the
            folder's name, or NAME if -n is given. Snapshotting the same
            folder again reuses the entry; a different -o file updates it; a
            second folder with the same base name gets a suffixed name
            (test1-2). A control file already registered for another folder
            is refused.

            Without a DIR, every target of the config file is snapshotted
            (only the ones named with -t if given). A control file stored
            inside its own monitored folder is excluded from the scan.
            Missing control_dir directories are created.
        """,
        "options": [
            ("DIR", "Folder to snapshot (recursively)"),
            ("-o, --output CONTROL_FILE", "Where to write the control file (default: DIR.ctl)"),
            ("-n, --name NAME", "Target name recorded in the config (default: folder name)"),
            ("-f, --config INI", "Config file to use/register in instead of your own"),
            ("-t, --target NAME", "Only this target from the config (repeatable)"),
        ],
        "examples": [
            ("fmon test1", "snapshot ./test1 into ./test1.ctl and register target [test1]"),
            ("fmon test1 -o baselines/test1.ctl", "choose where the control file goes"),
            ("fmon /srv/www -n website", "register the folder under the name 'website'"),
            ("fmon snapshot", "snapshot every target defined in your config file"),
            ("fmon snapshot -t website -t usr", "only the named targets"),
            ("fmon snapshot -f /etc/fmon.ini", "all targets of a system-wide config (e.g. as root)"),
        ],
    },
    "check": {
        "summary": "Compare folder(s) with their control file(s) and report every change",
        "usage": [
            "fmon check DIR [-c CONTROL_FILE]",
            "fmon check -c DIR | -c CONTROL_FILE",
            "fmon check [-f INI] [-t NAME ...]",
            "fmon DIR -c [CONTROL_FILE]                 (shortcut)",
            "fmon -c DIR | -c CONTROL_FILE              (shortcut)",
        ],
        "description": """
            Rescans a folder and compares it with its control file. The
            report is titled with the folder's absolute path and lists files
            that were added, removed or modified anywhere in the tree.
            Modified files show size, permissions and mtime before and after,
            with the changed attributes marked by '*'.

            The folder and the control file may be given in either order,
            and one of them can be omitted when the control file is named
            after the folder (test1 <-> test1.ctl). Without a DIR the targets
            of the config file are checked, one report per target.

            The full report is also written to the audit log, so it is
            always possible to find out when a file appeared, disappeared or
            changed, and which command found it.
        """,
        "options": [
            ("DIR", "Folder to check"),
            ("-c, --control [CONTROL_FILE|DIR]", "Control file to compare with (or the folder, see above)"),
            ("-f, --config INI", "Config file to take the targets from instead of your own"),
            ("-t, --target NAME", "Only this target from the config (repeatable)"),
        ],
        "examples": [
            ("fmon test1 -c test1.ctl", "check ./test1 against ./test1.ctl"),
            ("fmon -c test1", "same: the control file test1.ctl is inferred"),
            ("fmon -c test1.ctl", "same: the folder test1 is inferred"),
            ("fmon test1 -c", "same"),
            ("fmon -c baselines/test1.ctl test1", "control file kept elsewhere"),
            ("fmon check", "check every target of your config file"),
            ("fmon check -t website", "only one target"),
            ("fmon check -f /etc/fmon.ini; echo $?", "exit status 1 means changes were found"),
        ],
    },
    "config": {
        "summary": "Show, query and edit the configuration file",
        "usage": [
            "fmon config [show] [NAME|PATTERN ...]",
            "fmon config init",
            "fmon config add NAME PATH [-c CONTROL_FILE]",
            "fmon config delete NAME|PATTERN ... [--ctl]",
            "fmon config set|update OPTION VALUE",
            "fmon config set|update SECTION OPTION VALUE",
            "fmon config unset OPTION | SECTION OPTION",
            "fmon config get [OPTION]",
            "fmon config move PATH",
            "(all actions accept -f INI to work on another config file)",
        ],
        "description": """
            Without an action (or with 'show' and no names) the whole config
            file is displayed: its location, whether you may modify it, its
            raw contents, the log file in use and every target with its
            resolved folder and control file. With names or glob patterns
            ('t*', 'web?') only the matching targets are shown, each with its
            raw entry and whether its control file exists.

            Edits are validated before anything is written - a duplicate
            name, two targets sharing a control file, an unknown option or a
            bad value is refused and the file is left untouched. Comments in
            the file are preserved.

            Options of the [fmon] section (control_dir, log_file,
            log_max_size, log_keep_days) can be set by name alone:
            'fmon config set log_max_size 5M'. Target options (path,
            control_file) take the target name first:
            'fmon config set website control_file web.ctl'. 'update' is a
            synonym of 'set'.

            If an administrator made the config read-only, users can still
            view it and snapshot/check its targets, but every edit - and
            snapshotting a new folder, which would add an entry - is refused
            with a "not permitted" message and logged as a Warning.
            'delete --ctl' deletes control files only after the config was
            edited successfully, so it too is reserved to whoever may modify
            the config.
        """,
        "options": [
            ("show [NAME|PATTERN ...]", "Show the file, or only the matching targets"),
            ("init", "Create a template config file"),
            ("add NAME PATH [-c FILE]", "Add a target (control file: FILE or <NAME>.ctl in control_dir)"),
            ("delete NAME|PATTERN ... [--ctl]", "Delete target(s); --ctl also deletes their control files"),
            ("set|update [SECTION] OPTION VALUE", "Set a [fmon] option, or a target option (path, control_file, schedule)"),
            ("unset [SECTION] OPTION", "Remove an option"),
            ("get [OPTION]", "Show the effective [fmon] settings (or one of them)"),
            ("move PATH", "Move your personal config file to PATH (a file or a directory) and use it from now on"),
            ("-f, --config INI", "Work on this config file instead of your own"),
        ],
        "examples": [
            ("fmon config", "show everything"),
            ("fmon config show test1", "only target test1"),
            ("fmon config show \"t*\" web?", "targets matching patterns (quote them in bash)"),
            ("fmon config init", "create your config file from the template"),
            ("fmon config add website /srv/www", "add a target -> control file website.ctl in control_dir"),
            ("fmon config add home /home -c /root/fmon/home.ctl", "with an explicit control file"),
            ("fmon config set control_dir /var/lib/fmon", "where control files go by default"),
            ("fmon config set log_file /var/log/fmon/fmon.log", "central audit log"),
            ("fmon config update log_max_size 5M", "rotate the log above 5 MB ('update' = 'set')"),
            ("fmon config set log_keep_days 60", "delete rotated logs older than 60 days (0 = keep forever)"),
            ("fmon config set log_file off", "disable logging"),
            ("fmon config set website control_file web-2026.ctl", "option of a target"),
            ("fmon config set website path /srv/www2", "point a target to another folder"),
            ("fmon config set website schedule 1h", "check it every hour (see 'fmon help watch')"),
            ("fmon config unset log_max_size", "back to the default (1M)"),
            ("fmon config unset website control_file", "back to <name>.ctl in control_dir"),
            ("fmon config get", "effective values of all [fmon] settings"),
            ("fmon config get log_file", "one setting"),
            ("fmon config move D:\\fmon\\fmon.ini", "keep your config elsewhere (remembered in your preferences)"),
            ("fmon config delete test1", "remove the entry, keep its control file"),
            ("fmon config delete \"test*\" --ctl", "remove entries and delete their control files"),
            ("fmon config -f /etc/fmon.ini add usr /usr", "edit a system-wide config"),
        ],
    },
    "log": {
        "summary": "Show the audit log of executed commands",
        "usage": [
            "fmon log [-n N] [-f INI]",
            "fmon log --files [-f INI]",
        ],
        "description": """
            Every command is recorded in the audit log with its type, time,
            user, command line and result, followed by the full report for
            checks:

                Info    | 2026-11-06 23:02:02 | kehsi | fmon -c test2 | 1 added, 0 removed, 0 modified
                    fmon check: C:\\Users\\kehsi\\test2
                    Control file: C:\\Users\\kehsi\\test2.ctl
                    Added: 1
                      another.txt  (size 34 B, perms 0666 (rw-rw-rw-))
                    Removed: 0
                    Modified: 0

            Types: Info (the command succeeded), Warning (a user attempted an
            action reserved to administrators), Error (the command failed:
            permission errors, broken command lines, missing folders or
            control files, invalid configuration).

            The log is fmon.log next to the config file unless log_file is
            set. When it would exceed log_max_size (default 1M) it is renamed
            after the period it covers, e.g.
            fmon_2026-11-06-21-00-05_to_2026-11-06-23-00-10.log. Rotated
            logs are kept forever unless log_keep_days is set: then those
            whose last entry is older than that many days are deleted.
        """,
        "options": [
            ("-n, --lines N", "Number of entries to show (default 20, 0 = all)"),
            ("--files", "List the rotated log files instead of entries"),
            ("-f, --config INI", "Show the log of another config file"),
        ],
        "examples": [
            ("fmon log", "the last 20 entries"),
            ("fmon log -n 0", "everything"),
            ("fmon log --files", "rotated log files and their sizes"),
            ("fmon log -f /etc/fmon.ini", "the log of a system-wide config"),
            ("fmon config set log_file /var/log/fmon/fmon.log", "move the log"),
        ],
    },
    "watch": {
        "summary": "Run the scheduled checks in the foreground (headless monitoring)",
        "usage": ["fmon watch [-i] [-v] [-f INI]"],
        "description": """
            Targets with a 'schedule' option (e.g. 'schedule = 15m') are
            checked at that interval for as long as 'fmon watch' runs (or
            the GUI is open). Each check is a normal 'fmon check -t NAME'
            run, so it is written to the audit log with its full report,
            tagged [scheduled]. Press Ctrl+C to stop.

            Set or change a schedule with 'fmon config set TARGET schedule
            INTERVAL' (units s, m, h, d; 'off' disables). The config file is
            re-read every 10 seconds, so schedule changes apply while
            watching.
        """,
        "options": [
            ("-i, --immediately", "Check every scheduled target right away, then follow the schedule"),
            ("-v, --verbose", "Print the full report when a check finds changes or fails"),
            ("-f, --config INI", "Config file whose scheduled targets to run"),
        ],
        "examples": [
            ("fmon config set website schedule 15m", "check [website] every 15 minutes"),
            ("fmon watch", "run the schedules of your config"),
            ("fmon watch -i -v", "check everything now, show reports with changes"),
            ("fmon watch -f /etc/fmon.ini", "headless monitoring of a system-wide config (e.g. as a service)"),
            ("fmon config set website schedule off", "stop scheduling a target"),
        ],
    },
    "gui": {
        "summary": "Open the graphical application",
        "usage": ["fmon gui [-f INI]", "fmon-gui"],
        "description": """
            A desktop application (Tk) with tabs for: Targets (snapshot,
            check, add, edit, delete, filter), Run (execute any fmon command
            and see its output), Schedule (per-target intervals, start/stop
            the scheduler, last/next run), Log (the audit log in pages of
            25/50/100 entries with filters by type, user, text, command and
            date range, with the full report of the selected entry) and
            Settings (the [fmon] options and the raw config file).

            Every action is executed through the normal command line, so it
            is audit-logged like a terminal command, tagged [gui]. A
            read-only config is respected: edits are refused with a "not
            permitted" message.
        """,
        "options": [("-f, --config INI", "Config file to open (default: your own)")],
        "examples": [
            ("fmon gui", "open the GUI on your config"),
            ("fmon gui -f /etc/fmon.ini", "open a system-wide config"),
            ("fmon-gui", "same as 'fmon gui' (installed launcher)"),
        ],
    },
    "help": {
        "summary": "Show this manual, or the manual of one command",
        "usage": ["fmon help [COMMAND]", "fmon", "fmon COMMAND -h"],
        "description": """
            'fmon' alone or 'fmon help' shows the complete manual through a
            pager (like 'man'); 'fmon help COMMAND' shows one command.
            'fmon COMMAND -h' gives the short option summary with examples.
        """,
        "options": [("COMMAND", "snapshot, check, config, log or help")],
        "examples": [
            ("fmon", "the full manual"),
            ("fmon help config", "everything about the config command"),
            ("fmon config -h", "short option summary of 'config'"),
        ],
    },
}

CONFIG_FILE_SECTION = """
    Every user has exactly one personal config file:

        Windows      %APPDATA%\\fmon\\fmon.ini
        Linux/macOS  ~/.config/fmon/fmon.ini  ($XDG_CONFIG_HOME/fmon/fmon.ini)

    It is created automatically the first time a control file is made, or
    with 'fmon config init'. Its location can be changed with 'fmon config
    move PATH' (or in the GUI's Settings tab) - the choice is remembered in
    preferences.json in the directory above - or overridden for one shell
    with the FMON_CONFIG environment variable. Another file can be used
    with -f INI, e.g. a system-wide /etc/fmon.ini maintained by root.
    Format:

        [fmon]
        control_dir  = /var/lib/fmon        ; default home of control files
        log_file     = /var/log/fmon.log    ; or "off"
        log_max_size = 1M                   ; rotate above this size (K/M/G)
        log_keep_days = 60                  ; delete rotated logs older than 60 days (0 = keep)

        [usr]                               ; one section per target
        path = /usr                         ; -> /var/lib/fmon/usr.ctl

        [home]
        path = /home
        control_file = home-baseline.ctl    ; relative -> inside control_dir
        schedule = 15m                      ; checked every 15 min by watch/GUI

    Rules: target names are unique and may contain letters, digits, '.',
    '_' and '-'; every target needs a path; no two targets may share a
    control file (checked after resolving symlinks and case); relative
    paths are resolved against the directory of the config file.

    The same folder may be monitored by several users at once, each with
    their own control files in their own config.
"""

SECURITY_SECTION = """
    Control files are the trusted baseline and should live where the
    audited users cannot modify them, e.g. (as root):

        mkdir -m 755 /var/lib/fmon
        fmon snapshot -f /etc/fmon.ini

    Regular users can then neither replace the control files nor alter the
    baseline, while root can run 'fmon check -f /etc/fmon.ini' at any time
    to get a report of what changed - and the audit log keeps every report.

    Making a user's config file read-only locks its targets: the user can
    still snapshot and check them, but cannot add, change or delete
    targets, change the log settings, or create new control files.
"""

EXIT_STATUS_SECTION = """
    0   success; for 'check': no changes detected
    1   'check': changes were detected in at least one target
    2   error: bad command line, missing folder or control file, invalid
        or unmodifiable configuration, ... (errors in one target do not
        stop the other targets from being processed)
"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _para(text, indent=INDENT):
    """Re-wrap a docstring-style paragraph block, keeping blank-line breaks
    and leaving pre-indented (example) lines untouched."""
    out = []
    for block in textwrap.dedent(text).strip("\n").split("\n\n"):
        lines = block.split("\n")
        if all(l.startswith("    ") or not l.strip() for l in lines):
            out.append("\n".join(indent + l for l in lines))  # preformatted
        else:
            out.append(textwrap.fill(" ".join(l.strip() for l in lines), WIDTH,
                                     initial_indent=indent, subsequent_indent=indent))
    return "\n\n".join(out)


def _table(rows, indent=INDENT):
    """Two-column table; long descriptions wrap under themselves."""
    width = min(max(len(k) for k, _ in rows), 36)
    desc_width = max(WIDTH - len(indent) - width - 2, 30)
    lines = []
    for key, desc in rows:
        wrapped = textwrap.wrap(desc, desc_width) or [""]
        if len(key) > width:
            lines.append(f"{indent}{key}")
            first = f"{indent}{'':<{width}}  {wrapped[0]}"
        else:
            first = f"{indent}{key:<{width}}  {wrapped[0]}"
        lines.append(first)
        lines += [f"{indent}{'':<{width}}  {more}" for more in wrapped[1:]]
    return "\n".join(lines)


def _examples(rows, indent=INDENT):
    lines = []
    for cmd, why in rows:
        lines.append(f"{indent}$ {cmd}")
        lines.append(f"{indent}      {why}")
    return "\n".join(lines)


def examples_text(command):
    """Examples of a command, for argparse epilogs."""
    return "examples:\n" + _examples(COMMANDS[command]["examples"], indent="  ")


def description_text(command):
    """Description of a command re-flowed to full width, for argparse."""
    return _para(COMMANDS[command]["description"], indent="")


def render_command(name):
    """Manual page of one command."""
    cmd = COMMANDS[name]
    parts = [
        f"FMON {name.upper()}",
        "",
        "NAME",
        f"{INDENT}fmon {name} - {cmd['summary']}",
        "",
        "SYNOPSIS",
        "\n".join(INDENT + u for u in cmd["usage"]),
        "",
        "DESCRIPTION",
        _para(cmd["description"]),
        "",
        "OPTIONS",
        _table(cmd["options"]),
        "",
        "EXAMPLES",
        _examples(cmd["examples"]),
        "",
    ]
    return "\n".join(parts)


def render_manual():
    """The complete manual."""
    user_ini = fmon_config.user_config_path()
    parts = [
        "FMON(1)                          User Commands                          FMON(1)",
        "",
        "NAME",
        f"{INDENT}fmon - folder audit: snapshot folders and detect changes",
        "",
        "SYNOPSIS",
        f"{INDENT}fmon DIR [-o CONTROL_FILE]            create a control file (snapshot)",
        f"{INDENT}fmon DIR -c [CONTROL_FILE]            check a folder against it",
        f"{INDENT}fmon -c DIR | -c CONTROL_FILE         same, inferring the other one",
        f"{INDENT}fmon snapshot | check [-t NAME ...]   all/selected targets of your config",
        f"{INDENT}fmon config ...                       show, query and edit the config",
        f"{INDENT}fmon log [-n N]                       the audit log",
        f"{INDENT}fmon watch                            run scheduled checks (headless)",
        f"{INDENT}fmon gui                              the graphical application",
        f"{INDENT}fmon help [COMMAND]                   this manual",
        "",
        "DESCRIPTION",
        _para("""
            fmon records the state of a folder tree - every file's size,
            permissions and modification time - in a control file, and later
            reports exactly which files were added, removed or modified. Any
            number of folders can be protected, each with its own control
            file, all defined in a configuration file that fmon maintains for
            you. Every command is written to an audit log.

            Typical workflow:
        """),
        _examples([
            ("fmon /srv/www -n website", "take the baseline; target [website] is registered"),
            ("fmon check -t website", "later: what changed?"),
            ("fmon log", "who ran what, when, and what was found"),
        ]),
        "",
        "COMMANDS",
        _table([(f"fmon {n}", c["summary"]) for n, c in COMMANDS.items()]),
        "",
        f"{INDENT}A first argument that is not a command means 'snapshot' ('check' if -c",
        f"{INDENT}is present): 'fmon test1' == 'fmon snapshot test1'.",
        "",
    ]
    for name in COMMANDS:
        cmd = COMMANDS[name]
        parts += [
            f"{name.upper()}",
            "\n".join(INDENT + u for u in cmd["usage"]),
            "",
            _para(cmd["description"]),
            "",
            _table(cmd["options"]),
            "",
            f"{INDENT}Examples:",
            _examples(cmd["examples"], indent=INDENT + "  "),
            "",
        ]
    parts += [
        "CONFIGURATION FILE",
        CONFIG_FILE_SECTION.strip("\n"),
        "",
        "SECURITY",
        SECURITY_SECTION.strip("\n"),
        "",
        "EXIT STATUS",
        EXIT_STATUS_SECTION.strip("\n"),
        "",
        "FILES",
        _table([
            (user_ini, "your config file"),
            (os.path.join(os.path.dirname(user_ini), "fmon.log"), "your audit log (default)"),
            (os.path.join(os.path.dirname(user_ini), "controls"), "default control_dir of a new config"),
        ]),
        "",
        "SEE ALSO",
        f"{INDENT}fmon help COMMAND, fmon COMMAND -h, README.md, fmon.ini.example",
        "",
    ]
    return "\n".join(parts)


def page(text):
    """Show text through a pager when attached to a terminal (like `man`);
    print it plainly otherwise (pipes, redirects, tests)."""
    if sys.stdout.isatty():
        pydoc.pager(text)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
