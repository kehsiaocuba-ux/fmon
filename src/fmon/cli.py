import argparse
import os
import sys
import time
from datetime import datetime

import fmon
from fmon import config as fmon_config
from fmon import log as fmon_log
from fmon import manual
from fmon import schedule as fmon_schedule
from fmon.config import ConfigError, ConfigPermissionError, Target, load_config, CONTROL_EXT, CONFIG_TEMPLATE
from fmon.scanner import FolderScanner, has_changes, format_mode

EXIT_OK = 0
EXIT_CHANGES = 1
EXIT_ERROR = 2

COMMANDS = ("snapshot", "check", "config", "log", "help", "watch", "gui")
HELP_FLAGS = ("-h", "--help", "-V", "--version")


class UsageError(Exception):
    """A broken command line (raised instead of argparse's immediate exit so
    it can be logged)."""

    def __init__(self, parser, message):
        super().__init__(message)
        self.parser = parser


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(self, message)


class Session:
    """Collects what a command did, for the exit status and the audit log."""

    def __init__(self):
        self.status = EXIT_OK
        self.results = []
        self.details = []            # full output worth keeping (check reports)
        self.not_permitted = False   # an admin-only action was attempted

    def info(self, message, log=None):
        print(message)
        self.results.append(log if log is not None else message)

    def error(self, message, hint=None):
        """Report a failure; `hint` is shown to the user but not logged."""
        print(f"error: {message}", file=sys.stderr)
        if hint:
            print(f"       {hint}", file=sys.stderr)
        self.results.append(str(message))
        self.status = EXIT_ERROR

    def changes(self):
        if self.status == EXIT_OK:
            self.status = EXIT_CHANGES

    @property
    def level(self):
        if self.not_permitted:
            return fmon_log.WARNING
        if self.status == EXIT_ERROR:
            return fmon_log.ERROR
        return fmon_log.INFO

    @property
    def summary(self):
        return "; ".join(r for r in self.results if r) or "ok"

    @property
    def detail_text(self):
        return "\n".join(self.details)


_session = Session()

# -c given without a value ("fmon test1 -c"): use the default control file.
DEFAULT_CONTROL = ""



# --- report -------------------------------------------------------------------

def _fmt_time(ts):
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_size(size):
    return "?" if size is None else f"{size} B"


def _entry_summary(entry):
    return f"size {_fmt_size(entry.get('size'))}, perms {format_mode(entry.get('mode'))}"


def _change_line(label, before, after, fmt):
    marker = "" if before == after else "  *"
    return f"      {label:<7}{fmt(before)} -> {fmt(after)}{marker}"


def format_report(root_dir, control_path, diff, name=None):
    """The check report as a list of lines."""
    label = f" [{name}]" if name else ""
    title = f"fmon check{label}: {os.path.abspath(root_dir)}"
    lines = [title, "=" * len(title), f"Control file: {os.path.abspath(control_path)}", ""]

    lines.append(f"Added: {len(diff['added'])}")
    for path, entry in diff["added"]:
        lines.append(f"  {path}  ({_entry_summary(entry)})")

    lines.append(f"Removed: {len(diff['removed'])}")
    for path, entry in diff["removed"]:
        lines.append(f"  {path}  ({_entry_summary(entry)})")

    lines.append(f"Modified: {len(diff['modified'])}")
    for path, before, after in diff["modified"]:
        lines.append(f"  {path}")
        lines.append(_change_line("size:", before.get("size"), after.get("size"), _fmt_size))
        lines.append(_change_line("perms:", before.get("mode"), after.get("mode"), format_mode))
        lines.append(_change_line("mtime:", before.get("mtime"), after.get("mtime"), _fmt_time))

    if not has_changes(diff):
        lines += ["", "No changes detected."]
    return lines


def print_report(root_dir, control_path, diff, name=None):
    """Print the check report and keep it for the audit log."""
    lines = format_report(root_dir, control_path, diff, name=name)
    print("\n".join(lines))
    # the log keeps everything except decoration (underline, blank lines)
    _session.details.append("\n".join(l for l in lines[:1] + lines[2:] if l.strip()))


def _error(msg):
    _session.error(msg)


def _label(target):
    return f"[{target.name}] " if target.name else ""


# --- snapshot / check ---------------------------------------------------------

def run_snapshot(targets, register_in=None, name=None):
    """Snapshot each target. For an ad-hoc directory (register_in given),
    the folder/control file pair is recorded in that config file first, so
    every control file fmon creates is known to the configuration."""
    status = EXIT_OK
    for t in targets:
        if not os.path.isdir(t.path):
            _error(f"{_label(t)}Directory not found: {t.path}")
            status = EXIT_ERROR
            continue
        undo = None
        if register_in:
            t, action, undo = fmon_config.register_target(register_in, t.path, t.control_file, name=name)
        try:
            out = FolderScanner.for_target(t).save_control_file(t.control_file)
        except OSError as e:
            _error(f"{_label(t)}cannot write control file {t.control_file}: {e.strerror or e}")
            status = EXIT_ERROR
            if undo:
                undo()  # leave the config as it was
            continue
        _session.info(f"{_label(t)}Snapshot of {t.path} saved to {out}",
                      log=f"{_label(t)}snapshot of {t.path} saved to {out}")
        if register_in and action != "unchanged":
            _session.info(f"[{t.name}] Target {action} in {os.path.abspath(register_in)}",
                          log=f"target [{t.name}] {action} in config")
    return status


def run_check(targets):
    status = EXIT_OK
    first = True
    for t in targets:
        if not first:
            print()
        first = False
        try:
            diff = FolderScanner.for_target(t).check(t.control_file)
        except FileNotFoundError as e:
            # distinguish "no control file yet" from "folder missing"
            msg = str(e) if "Directory not found" in str(e) else f"Control file not found: {t.control_file}"
            _error(f"{_label(t)}{msg}")
            status = EXIT_ERROR
            continue
        except (PermissionError, OSError, ValueError) as e:
            _error(f"{_label(t)}{e}")
            status = EXIT_ERROR
            continue
        print_report(t.path, t.control_file, diff, name=t.name)
        if has_changes(diff):
            _session.results.append(
                f"{_label(t)}{t.path}: {len(diff['added'])} added, "
                f"{len(diff['removed'])} removed, {len(diff['modified'])} modified"
            )
            if status == EXIT_OK:
                status = EXIT_CHANGES
        else:
            _session.results.append(f"{_label(t)}{t.path}: no changes")
    return status


# --- config -------------------------------------------------------------------

def _config_path(args):
    return os.path.abspath(args.config) if getattr(args, "config", None) else fmon_config.user_config_path()


def _control_status(control_file):
    try:
        st = os.stat(control_file)
    except OSError:
        return "missing"
    return f"exists, {st.st_size} B, modified {_fmt_time(st.st_mtime)}"


def _print_targets(targets):
    if not targets:
        print("Targets: (none)")
        return
    print("Targets:")
    for t in targets:
        print(f"  [{t.name}]")
        print(f"    path:         {t.path}")
        print(f"    control file: {t.control_file}  ({_control_status(t.control_file)})")
        if t.schedule:
            print(f"    schedule:     every {fmon_config.format_interval(t.schedule)}")


def _select_targets(targets, patterns):
    """Targets matching any of the names/glob patterns, in config order.
    Raises ConfigError for a pattern that matches nothing."""
    selected = []
    for pattern in patterns:
        matches = fmon_config.match_targets(targets, pattern)
        if not matches:
            kind = "no target matches" if fmon_config.is_pattern(pattern) else "no target named"
            raise ConfigError(f"{kind} '{pattern}'")
        for t in matches:
            if t not in selected:
                selected.append(t)
    return [t for t in targets if t in selected]


def config_show(args):
    path = _config_path(args)
    print(f"Config file: {path}")
    if not os.path.isfile(path):
        print("  (not found - create it with 'fmon config init' or by taking a snapshot)")
        _session.results.append(f"config file not found: {path}")
        return EXIT_ERROR
    print(f"Writable by you: {'yes' if fmon_config.is_writable(path) else 'no (read-only)'}")
    settings = fmon_config.log_settings(path)
    print(f"Log file: {settings.path or 'disabled'}")
    for section, option, message in fmon_config.config_warnings(path):
        print(f"Warning: [{section}] {option}: {message}")
    print()
    text = fmon_config.read_text(path)
    targets = load_config(path, allow_empty=True)

    patterns = getattr(args, "pattern", None) or []
    if not patterns:
        print("--- contents " + "-" * 50)
        print(text.rstrip("\n"))
        print("-" * 63)
        print()
        _print_targets(targets)
        _session.results.append(f"showed config with {len(targets)} target(s)")
        return EXIT_OK

    selected = _select_targets(targets, patterns)
    ini = fmon_config.IniText(text)
    shown = ", ".join(patterns)
    print(f"Targets matching {shown}: {len(selected)}")
    _session.results.append(f"showed {len(selected)} target(s) matching {shown}")
    for t in selected:
        print()
        print(ini.section_text(t.name).rstrip("\n"))
        print(f"  -> folder:       {t.path}")
        print(f"  -> control file: {t.control_file}  ({_control_status(t.control_file)})")
        print(f"  -> schedule:     {'every ' + fmon_config.format_interval(t.schedule) if t.schedule else 'off'}")
    return EXIT_OK


def config_init(args):
    path = _config_path(args)
    if os.path.exists(path):
        _error(f"config file already exists: {path}")
        return EXIT_ERROR
    if not fmon_config.is_writable(path):
        raise ConfigPermissionError(f"you are not permitted to create {path}")
    fmon_config.write_text(path, CONFIG_TEMPLATE)
    _session.info(f"Created {path}", log=f"created config {path}")
    return EXIT_OK


def config_add(args):
    path = _config_path(args)
    targets = fmon_config.add_target(path, args.name, args.path, args.control_file)
    t = next(t for t in targets if t.name == args.name)
    _session.info(f"Added target [{t.name}]: {t.path} -> {t.control_file}",
                  log=f"added target [{t.name}] {t.path} -> {t.control_file}")
    return EXIT_OK


def config_delete(args):
    """Delete target(s) from the config; with --ctl also delete their control
    files. The config is edited first, so deleting control files is only
    possible for users permitted to modify the config."""
    path = _config_path(args)
    targets = load_config(path, allow_empty=True)
    selected = _select_targets(targets, args.names)
    status = EXIT_OK
    for t in selected:
        fmon_config.remove_target(path, t.name)
        if not args.ctl:
            _session.info(f"Deleted target [{t.name}] (control file kept: {t.control_file})",
                          log=f"deleted target [{t.name}], kept {t.control_file}")
            continue
        try:
            os.remove(t.control_file)
            _session.info(f"Deleted target [{t.name}] and its control file {t.control_file}",
                          log=f"deleted target [{t.name}] and {t.control_file}")
        except FileNotFoundError:
            _session.info(f"Deleted target [{t.name}] (control file {t.control_file} did not exist)",
                          log=f"deleted target [{t.name}], {t.control_file} did not exist")
        except PermissionError:
            _error(f"[{t.name}] target deleted, but you are not permitted to delete {t.control_file}")
            status = EXIT_ERROR
        except OSError as e:
            _error(f"[{t.name}] target deleted, but the control file could not be deleted: {e}")
            status = EXIT_ERROR
    return status


def _section_option(parts, with_value, action):
    """Interpret 'OPTION [VALUE]' (a [fmon] setting) or 'SECTION OPTION [VALUE]'."""
    n = 2 if with_value else 1
    if len(parts) == n:
        option = parts[0]
        known = set(fmon_config.GLOBAL_OPTIONS) | (set() if with_value else set(fmon_config.DEPRECATED_OPTIONS))
        if option.lower() not in known:
            raise ConfigError(
                f"'{option}' is not a [{fmon_config.GLOBAL_SECTION}] setting "
                f"({', '.join(fmon_config.GLOBAL_OPTIONS)}); "
                f"for a target use: fmon config {action} TARGET OPTION{' VALUE' if with_value else ''}"
            )
        return (fmon_config.GLOBAL_SECTION, *parts)
    if len(parts) == n + 1:
        return tuple(parts)
    raise ConfigError(
        f"usage: fmon config {action} OPTION{' VALUE' if with_value else ''}  |  "
        f"fmon config {action} SECTION OPTION{' VALUE' if with_value else ''}"
    )


def config_set(args):
    path = _config_path(args)
    section, option, value = _section_option(args.args, True, "set")
    fmon_config.set_option(path, section, option, value)
    _session.info(f"Set [{section}] {option} = {value}", log=f"set [{section}] {option} = {value}")
    return EXIT_OK


def config_unset(args):
    path = _config_path(args)
    section, option = _section_option(args.args, False, "unset")
    fmon_config.unset_option(path, section, option)
    _session.info(f"Removed [{section}] {option}", log=f"unset [{section}] {option}")
    return EXIT_OK


def config_move(args):
    """Relocate the personal config file and remember the new location."""
    if getattr(args, "config", None):
        raise ConfigError("'config move' relocates your personal config; it cannot be combined with -f")
    old = fmon_config.user_config_path()
    had_file = os.path.isfile(old)
    new = fmon_config.relocate_user_config(args.path)
    if new == old:
        _session.info(f"Config file already at {new}", log="config location unchanged")
        return EXIT_OK
    if had_file and not os.path.isfile(old):
        how = "moved"
    elif os.path.isfile(new):
        how = "created" if not had_file else "existing file used"
    else:
        how = "will be created there"
    _session.info(f"Config file is now {new} ({how})", log=f"config moved from {old} to {new} ({how})")
    return EXIT_OK


def config_get(args):
    """Show the effective [fmon] settings."""
    path = _config_path(args)
    settings = fmon_config.global_settings(path)
    wanted = [o.lower() for o in args.options] if args.options else list(settings)
    unknown = [o for o in wanted if o not in settings]
    if unknown:
        raise ConfigError(
            f"unknown setting(s): {', '.join(unknown)} (available: {', '.join(settings)})"
        )
    print(f"Config file: {path}" + ("" if os.path.isfile(path) else "  (not found - showing defaults)"))
    for section, option, message in fmon_config.config_warnings(path):
        print(f"  Warning: [{section}] {option}: {message}")
    width = max(len(o) for o in wanted)
    for option in wanted:
        raw, effective = settings[option]
        if raw is None:
            print(f"  {option:<{width}} = {effective}  (default)")
        elif raw == effective:
            print(f"  {option:<{width}} = {raw}")
        else:
            print(f"  {option:<{width}} = {raw}  -> {effective}")
    _session.results.append(f"showed setting(s) {', '.join(wanted)}")
    return EXIT_OK


def run_log(args):
    """Show the last entries of the audit log."""
    settings = fmon_config.log_settings(_config_path(args))
    if not settings.path:
        _session.error("logging is disabled in the config (log_file = off)")
        return EXIT_ERROR
    print(f"Log file: {settings.path}")
    if args.files:
        rotated = fmon_log.rotated_files(settings.path)
        print(f"Rotated logs: {len(rotated)}")
        for path in rotated:
            print(f"  {os.path.basename(path)}  ({os.path.getsize(path)} B)")
        _session.results.append(f"listed {len(rotated)} rotated log file(s)")
        return EXIT_OK
    if not os.path.isfile(settings.path):
        print("  (no entries yet)")
        _session.results.append("log file empty")
        return EXIT_OK
    try:
        entries = fmon_log.tail(settings.path, args.lines)
    except PermissionError:
        _session.error(f"you are not permitted to read {settings.path}")
        return EXIT_ERROR
    for entry in entries:
        print(entry)
    _session.results.append(f"showed {len(entries)} log entries")
    return EXIT_OK


CONFIG_ACTIONS = {
    None: config_show,
    "show": config_show,
    "init": config_init,
    "add": config_add,
    "delete": config_delete,
    "remove": config_delete,
    "set": config_set,
    "update": config_set,
    "unset": config_unset,
    "get": config_get,
    "move": config_move,
}


def run_config(args):
    if getattr(args, "init", False):
        args.action = "init"
    return CONFIG_ACTIONS[args.action](args)


# --- argument resolution ------------------------------------------------------

def _default_control_for(directory):
    """Default control file for a folder: a sibling named <folder>.ctl."""
    return os.path.abspath(directory.rstrip("/\\")) + CONTROL_EXT


def _resolve_dir_and_control(dir_arg, control_arg, parser):
    """Work out (directory, control_file) for 'check' from the positional
    and -c values, whichever order they were given in.

    Accepted forms:
      fmon DIR -c FILE   fmon -c DIR   fmon -c FILE   fmon DIR -c   fmon -c FILE DIR
    """
    values = [v for v in (dir_arg, control_arg) if v]
    dirs = [v for v in values if os.path.isdir(v)]
    others = [v for v in values if not os.path.isdir(v)]

    if len(dirs) == 2:
        parser.error(f"expected a directory and a control file, got two directories: {dirs[0]}, {dirs[1]}")
    if len(others) == 2:
        parser.error(f"neither {others[0]} nor {others[1]} is a directory")

    directory = dirs[0] if dirs else None
    control = others[0] if others else None

    if directory is None:
        # only a control file given: infer the folder from its name
        stem = control[:-len(CONTROL_EXT)] if control.lower().endswith(CONTROL_EXT) else control
        if not os.path.isdir(stem):
            if not os.path.isfile(control):
                parser.error(f"{control} is neither a directory nor an existing control file")
            parser.error(f"cannot infer the folder for {control}; use: fmon DIR -c {control}")
        directory = stem
    if control is None:
        control = _default_control_for(directory)
    return os.path.abspath(directory), os.path.abspath(control)


def _load_targets(config_path, names):
    """Load targets from a config file, optionally filtering by name."""
    targets = load_config(config_path)
    if names:
        by_name = {t.name: t for t in targets}
        unknown = [n for n in names if n not in by_name]
        if unknown:
            raise ConfigError(f"unknown target(s): {', '.join(unknown)}")
        targets = [by_name[n] for n in names]
    return targets


def _resolve_targets(args, parser):
    """Turn CLI arguments into a list of Targets.

    Priority: explicit directory/control file > --config FILE > the user's own .ini.
    """
    dir_arg = args.dir
    control_arg = getattr(args, "control", None)
    output_arg = getattr(args, "output", None)

    if args.config and args.command == "check" and (dir_arg or control_arg is not None):
        parser.error("give either a directory or --config, not both")

    if not dir_arg and control_arg is None:
        # config mode
        if output_arg is not None:
            parser.error("--output only applies when a directory is given")
        if args.config:
            return _load_targets(args.config, args.target)
        path = fmon_config.user_config_path()
        if not os.path.isfile(path):
            raise ConfigError(
                f"no directory given and user config file not found: {path}\n"
                "       create it with 'fmon config init' or pass a directory / -f INI"
            )
        return _load_targets(path, args.target)

    if args.target:
        parser.error("--target requires a config file, not a directory")

    if args.command == "snapshot":
        directory = os.path.abspath(dir_arg)
        control = os.path.abspath(output_arg) if output_arg else _default_control_for(dir_arg)
    else:
        directory, control = _resolve_dir_and_control(dir_arg, control_arg, parser)
    return [Target(name=None, path=directory, control_file=control)]


# --- parser -------------------------------------------------------------------

def build_parser():
    parser = Parser(
        prog="fmon",
        description=(
            "fmon - folder audit: snapshot folders and detect changes.\n\n"
            "Run 'fmon' alone or 'fmon help' for the full manual with examples,\n"
            "'fmon help COMMAND' or 'fmon COMMAND -h' for one command."
        ),
        epilog=(
            "shortcuts:\n"
            "  fmon DIR [-o FILE]        same as: fmon snapshot DIR (default FILE: DIR.ctl)\n"
            "  fmon DIR -c [FILE]        same as: fmon check DIR -c FILE (default FILE: DIR.ctl)\n"
            "  fmon -c DIR | -c FILE     check a folder, or a control file named after it\n"
            "  fmon snapshot / check     with no directory uses your config file:\n"
            f"                            {fmon_config.user_config_path()}\n\n"
            "examples:\n"
            "  fmon test1                          baseline of ./test1 -> test1.ctl, target [test1]\n"
            "  fmon -c test1                       what changed in ./test1 since then?\n"
            "  fmon config show \"t*\"               targets whose name starts with t\n"
            "  fmon config set log_max_size 5M     change a setting\n"
            "  fmon log                            who did what, when, with which result"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"fmon {fmon.__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add_command(name, **kwargs):
        return sub.add_parser(
            name, help=manual.COMMANDS[name]["summary"],
            description=manual.description_text(name), epilog=manual.examples_text(name),
            formatter_class=argparse.RawDescriptionHelpFormatter, **kwargs,
        )

    def add_config_arg(p, help_text):
        p.add_argument("-f", "--config", metavar="INI", help=help_text)

    def add_source_args(p):
        p.add_argument("dir", nargs="?", help="Directory to scan (recursively, including subfolders)")
        add_config_arg(p, "Read targets from this .ini file instead of your user config")
        p.add_argument(
            "-t", "--target", action="append", metavar="NAME",
            help="Only process this target from the config (repeatable)",
        )

    snap = add_command("snapshot")
    add_source_args(snap)
    snap.add_argument("-o", "--output", metavar="CONTROL_FILE",
                      help="Output control file for a directory (default: <DIR>.ctl)")
    snap.add_argument("-n", "--name", metavar="NAME",
                      help="Target name to record in the config (default: folder name)")

    chk = add_command("check")
    add_source_args(chk)
    chk.add_argument(
        "-c", "--control", nargs="?", const=DEFAULT_CONTROL, metavar="CONTROL_FILE|DIR",
        help="Control file (or directory) to check; defaults to <DIR>.ctl",
    )

    lg = add_command("log")
    lg.add_argument("-n", "--lines", type=int, default=20, metavar="N",
                    help="Number of entries to show (default: 20, 0 = all)")
    lg.add_argument("--files", action="store_true", help="List the rotated log files instead")
    add_config_arg(lg, "Config file whose log to show")

    watch = add_command("watch")
    add_config_arg(watch, "Config file whose scheduled targets to run")
    watch.add_argument("-i", "--immediately", action="store_true",
                       help="Check every scheduled target right away, then follow the schedule")
    watch.add_argument("-v", "--verbose", action="store_true",
                       help="Print the full report when a check finds changes or fails")
    watch.add_argument("--max-runtime", type=float, metavar="SECONDS", help=argparse.SUPPRESS)

    g = add_command("gui")
    add_config_arg(g, "Config file to open in the GUI")

    hlp = add_command("help")
    hlp.add_argument("topic", nargs="?", metavar="COMMAND", choices=list(manual.COMMANDS),
                     help="Command to describe (default: the whole manual)")

    cfg = add_command("config")
    add_config_arg(cfg, "Operate on this .ini file instead of your user config")
    cfg.add_argument("--init", action="store_true", help=argparse.SUPPRESS)  # alias of 'init'
    actions = cfg.add_subparsers(dest="action", metavar="ACTION")

    a = actions.add_parser("show", help="Show the config file, or only the targets matching NAME/PATTERN")
    a.add_argument("pattern", nargs="*", metavar="NAME|PATTERN",
                   help="Target name or glob pattern, e.g. test1 or 't*' (repeatable)")
    add_config_arg(a, "Config file to show")
    a = actions.add_parser("init", help="Create a template config file")
    add_config_arg(a, "Config file to create")
    a = actions.add_parser("add", help="Add a monitored folder")
    a.add_argument("name", help="Target name (letters, digits, '.', '_', '-')")
    a.add_argument("path", help="Folder to monitor")
    a.add_argument("-c", "--control-file", metavar="FILE", help="Control file (default: <name>.ctl in control_dir)")
    add_config_arg(a, "Config file to edit")
    a = actions.add_parser("delete", aliases=["remove"],
                           help="Delete target(s) from the config (--ctl: also delete their control files)")
    a.add_argument("names", nargs="+", metavar="NAME|PATTERN", help="Target name or glob pattern")
    a.add_argument("--ctl", action="store_true", help="Also delete the control file(s)")
    add_config_arg(a, "Config file to edit")
    for verb in ("set", "update"):
        a = actions.add_parser(
            verb, help="Set a [fmon] setting (OPTION VALUE) or a target option (TARGET OPTION VALUE)",
            usage=f"fmon config {verb} [-f INI] OPTION VALUE\n       fmon config {verb} [-f INI] TARGET OPTION VALUE",
        )
        a.add_argument("args", nargs="+", metavar="ARG",
                       help=f"OPTION VALUE  ({', '.join(fmon_config.GLOBAL_OPTIONS)})  or  TARGET OPTION VALUE  (path, control_file)")
        add_config_arg(a, "Config file to edit")
    a = actions.add_parser(
        "unset", help="Remove a [fmon] setting (OPTION) or a target option (TARGET OPTION)",
        usage="fmon config unset [-f INI] OPTION\n       fmon config unset [-f INI] TARGET OPTION",
    )
    a.add_argument("args", nargs="+", metavar="ARG", help="OPTION  or  TARGET OPTION")
    add_config_arg(a, "Config file to edit")
    a = actions.add_parser("move", help="Move your personal config file to PATH and use it from now on")
    a.add_argument("path", metavar="PATH", help="New file path or directory")
    a = actions.add_parser("get", help="Show the effective [fmon] settings")
    a.add_argument("options", nargs="*", metavar="OPTION",
                   help=f"Setting(s) to show (default: all of {', '.join(fmon_config.GLOBAL_OPTIONS)})")
    add_config_arg(a, "Config file to read")
    return parser


def run_watch(args):
    """Run the scheduled checks in the foreground until interrupted."""
    path = _config_path(args)
    targets = load_config(path)
    scheduled = [t for t in targets if t.schedule]
    if not scheduled:
        _session.error(f"no target has a schedule in {path} "
                       "(set one with: fmon config set TARGET schedule 15m)")
        return EXIT_ERROR
    print(f"fmon watch: {path}")
    for t in scheduled:
        print(f"  [{t.name}] every {fmon_config.format_interval(t.schedule)}: {t.path}")
    print("Press Ctrl+C to stop.")

    def on_event(event):
        stamp = datetime.now().strftime("%H:%M:%S")
        if event.kind == "started":
            print(f"{stamp} [{event.name}] checking...")
        elif event.kind == "finished":
            print(f"{stamp} [{event.name}] {event.state.status_text}: {event.state.last_summary}")
            if event.state.last_status != EXIT_OK and args.verbose:
                print(fmon_log.INDENT + event.state.last_output.replace("\n", "\n" + fmon_log.INDENT))
        elif event.kind == "error":
            print(f"{stamp} error: {event.message}")

    start_time = time.time()
    scheduler = fmon_schedule.Scheduler(path, on_event=on_event)
    if args.immediately:
        scheduler.reload()
        for name in scheduler.jobs:
            scheduler.run_now(name)
    scheduler.start()
    try:
        while scheduler.running:
            time.sleep(0.5)
            if args.max_runtime and time.time() - start_time > args.max_runtime:
                break
    except KeyboardInterrupt:
        print()
    finally:
        scheduler.stop()
    _session.results.append("watch stopped")
    return EXIT_OK


def run_gui(args):
    from fmon import gui
    gui.main(config_path=_config_path(args))
    return EXIT_OK


def run_help(args):
    manual.page(manual.render_command(args.topic) if args.topic else manual.render_manual())


def normalize_argv(argv):
    """Support the shortcut syntax without an explicit command.

    If the first argument is not a command or help flag, insert 'check'
    when -c/--control is present, else 'snapshot'.
    """
    if not argv or argv[0] in COMMANDS or argv[0] in HELP_FLAGS:
        return argv
    command = "check" if any(a in ("-c", "--control") or a.startswith("--control=") for a in argv) else "snapshot"
    return [command] + list(argv)


def _write_log(argv, args):
    """Append the outcome of this invocation to the audit log."""
    config_path = _config_path(args) if args is not None else fmon_config.user_config_path()
    settings = fmon_config.log_settings(config_path)
    command = fmon_log.format_command(argv)
    origin = os.environ.get(fmon_schedule.ORIGIN_ENV, "").strip()
    if origin:
        command = f"[{origin}] {command}"
    fmon_log.write_entry(settings, _session.level, command,
                         _session.summary, details=_session.detail_text)


def main(argv=None):
    global _session
    argv = list(sys.argv[1:] if argv is None else argv)
    _session = Session()
    parser = build_parser()
    if not argv:
        manual.page(manual.render_manual())
        return

    args = None
    try:
        args = parser.parse_args(normalize_argv(argv))
        if args.command == "help":
            run_help(args)
            return
        if args.command == "config":
            status = run_config(args)
        elif args.command == "log":
            status = run_log(args)
        elif args.command == "watch":
            status = run_watch(args)
        elif args.command == "gui":
            status = run_gui(args)
        elif args.command == "snapshot":
            targets = _resolve_targets(args, parser)
            # an ad-hoc directory snapshot is recorded in the config file
            # (-f INI selects which one; default: the user's own)
            register_in = _config_path(args) if args.dir else None
            status = run_snapshot(targets, register_in=register_in, name=args.name)
        else:
            targets = _resolve_targets(args, parser)
            status = run_check(targets)
        _session.status = max(_session.status, status)
    except UsageError as e:
        e.parser.print_usage(sys.stderr)
        _session.error(f"{e.parser.prog}: {e}")
    except ConfigPermissionError as e:
        _session.not_permitted = True
        _session.error(e, hint="ask an administrator to change the configuration")
    except ConfigError as e:
        _session.error(e)
    except SystemExit:
        raise  # e.g. -h/--help
    except Exception as e:  # unexpected failure: log it, then let it surface
        _session.error(f"internal error: {type(e).__name__}: {e}")
        _write_log(argv, args)
        raise

    _write_log(argv, args)
    if _session.status != EXIT_OK:
        sys.exit(_session.status)


if __name__ == "__main__":
    main()
