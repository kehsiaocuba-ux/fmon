"""Loading, validation and editing of fmon .ini configuration files.

Format:

    [fmon]                      ; optional global settings
    control_dir = /var/lib/fmon ; default location of control files
                                ; (defaults to the directory of the .ini)

    [usr]                       ; one section per monitored folder; the
    path = /usr                 ; section name is the target name
                                ; control file -> <control_dir>/usr.ctl

    [home]
    path = /home
    control_file = home.ctl     ; relative -> inside control_dir
                                ; absolute -> used as-is

Every target must have a unique name and a unique control file. Several
users may monitor the same folder, each with their own control file, but
each user has exactly one personal .ini file (see user_config_path()).
"""
import configparser
import fnmatch
import json
import os
import re
import shutil
from dataclasses import dataclass

GLOBAL_SECTION = "fmon"
GLOBAL_OPTIONS = ("control_dir", "log_file", "log_max_size", "log_keep_days")
# Options of earlier versions: tolerated (ignored) so an old config keeps working,
# but reported by 'config show' / the GUI with the advice to remove them.
DEPRECATED_OPTIONS = {
    "log_backups": "obsolete - rotated logs are now kept by age (log_keep_days); "
                   "remove it with: fmon config unset log_backups",
}
LOG_FILENAME = "fmon.log"
DEFAULT_LOG_MAX_SIZE = 1024 * 1024   # rotate when the log exceeds 1 MB
DEFAULT_LOG_KEEP_DAYS = 0            # rotated logs to keep: 0 = forever, N = the last N days
CONFIG_FILENAME = "fmon.ini"
PREFERENCES_FILENAME = "preferences.json"
CONFIG_ENV = "FMON_CONFIG"           # environment override of the user config path
TARGET_OPTIONS = ("path", "control_file", "schedule")
CONTROL_EXT = ".ctl"

# Target names become file names, so keep them to a safe character set
# (no path separators, no '..', no whitespace).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GLOB_CHARS = set("*?[")

CONFIG_TEMPLATE = """\
; fmon per-user configuration. One section per monitored folder.
; The section name is the target name and the default control file name.
; Targets are added here automatically whenever fmon creates a control file.
; A target with "schedule = 15m" is checked every 15 minutes by the GUI or by
; "fmon watch" (units: s, m, h, d; "off" disables).

[fmon]
; Control files without an absolute path are stored here (relative to this
; file). Use a directory other users cannot write to.
control_dir = controls
; Every command is logged to fmon.log next to this file. Override with:
; log_file = /var/log/fmon/fmon.log   (or "off" to disable)
; log_max_size = 1M                   (rotate above this size; K/M/G suffixes)
; log_keep_days = 60                  (delete rotated logs older than 60 days;
;                                      0 = keep them forever)
"""


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed or inconsistent."""


class ConfigPermissionError(ConfigError):
    """Raised when the user is not allowed to modify the configuration file."""


@dataclass(frozen=True)
class Target:
    name: str
    path: str           # absolute path of the monitored folder
    control_file: str   # absolute path of its control file
    schedule: int = None  # seconds between scheduled checks, None = not scheduled


_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value):
    """'30s', '15m', '2h', '1d', plain seconds, or 'off' (-> None)."""
    text = str(value).strip().lower()
    if text in ("", "off", "none", "0", "never"):
        return None
    m = re.match(r"^(\d+)\s*([smhd]?)$", text)
    if not m:
        raise ValueError(f"invalid interval '{value}' (use e.g. 30s, 15m, 2h, 1d or off)")
    seconds = int(m.group(1)) * _INTERVAL_UNITS[m.group(2) or "s"]
    if seconds < 5:
        raise ValueError(f"interval '{value}' is too short (minimum 5s)")
    return seconds


def format_interval(seconds):
    if not seconds:
        return "off"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True)
class LogSettings:
    path: str           # absolute path of the log file, or None if disabled
    max_size: int       # rotate when the file would exceed this many bytes (0 = never)
    keep_days: int      # delete rotated logs older than this many days (0 = keep forever)


_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
               "g": 1024 ** 3, "gb": 1024 ** 3}


def parse_size(value):
    """'1048576', '512K', '1M', '2 GB' -> bytes."""
    m = re.match(r"^\s*(\d+)\s*([A-Za-z]*)\s*$", str(value))
    if not m or m.group(2).lower() not in _SIZE_UNITS:
        raise ValueError(f"invalid size '{value}' (use bytes or K/M/G suffix)")
    return int(m.group(1)) * _SIZE_UNITS[m.group(2).lower()]


def default_log_path(ini_path):
    return os.path.join(os.path.dirname(os.path.abspath(ini_path)), LOG_FILENAME)


def _log_settings_from_parser(parser, ini_path):
    ini_path = os.path.abspath(ini_path)
    ini_dir = os.path.dirname(ini_path)
    section = parser[GLOBAL_SECTION] if parser.has_section(GLOBAL_SECTION) else {}
    raw = section.get("log_file", "").strip()
    if raw.lower() in ("off", "none", "false"):
        path = None
    elif raw:
        path = os.path.abspath(os.path.join(ini_dir, raw))
    else:
        path = default_log_path(ini_path)
    try:
        max_size = parse_size(section.get("log_max_size", DEFAULT_LOG_MAX_SIZE))
    except ValueError as e:
        raise ConfigError(f"{ini_path}: [{GLOBAL_SECTION}] log_max_size: {e}") from None
    try:
        keep_days = int(section.get("log_keep_days", DEFAULT_LOG_KEEP_DAYS))
        if keep_days < 0:
            raise ValueError
    except ValueError:
        raise ConfigError(
            f"{ini_path}: [{GLOBAL_SECTION}] log_keep_days must be a non-negative integer (0 = keep forever)"
        ) from None
    return LogSettings(path=path, max_size=max_size, keep_days=keep_days)


def global_settings(ini_path):
    """Effective [fmon] settings: {option: (raw value or None, effective value)}.
    Works for a missing file too (all defaults)."""
    ini_path = os.path.abspath(ini_path)
    ini_dir = os.path.dirname(ini_path)
    parser = read_parser(ini_path) if os.path.isfile(ini_path) else _new_parser()
    section = parser[GLOBAL_SECTION] if parser.has_section(GLOBAL_SECTION) else {}
    raw = {opt: (section.get(opt, "").strip() or None) for opt in GLOBAL_OPTIONS}
    logs = _log_settings_from_parser(parser, ini_path)
    control_dir = os.path.abspath(os.path.join(ini_dir, raw["control_dir"])) if raw["control_dir"] else ini_dir
    return {
        "control_dir": (raw["control_dir"], control_dir),
        "log_file": (raw["log_file"], logs.path or "off"),
        "log_max_size": (raw["log_max_size"], f"{logs.max_size} bytes" if logs.max_size else "0 (never rotate)"),
        "log_keep_days": (raw["log_keep_days"],
                          f"{logs.keep_days} days" if logs.keep_days else "0 (keep rotated logs forever)"),
    }


def log_settings(ini_path):
    """Log settings for a config file. Best effort: if the file is missing
    or unreadable/invalid, the defaults are returned so logging still works."""
    ini_path = os.path.abspath(ini_path)
    try:
        parser = read_parser(ini_path)
        return _log_settings_from_parser(parser, ini_path)
    except ConfigError:
        return LogSettings(path=default_log_path(ini_path),
                           max_size=DEFAULT_LOG_MAX_SIZE, keep_days=DEFAULT_LOG_KEEP_DAYS)


def user_config_dir():
    """The per-user fmon directory (default home of the config, log and
    preferences).

    Windows: %APPDATA%\\fmon
    Others:  $XDG_CONFIG_HOME/fmon (default ~/.config/fmon)
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "fmon")


def default_user_config_path():
    return os.path.join(user_config_dir(), CONFIG_FILENAME)


def preferences_path():
    return os.path.join(user_config_dir(), PREFERENCES_FILENAME)


def load_preferences():
    """User preferences (GUI layout, appearance, config location) as a dict."""
    try:
        with open(preferences_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(prefs):
    path = preferences_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2, sort_keys=True)


def set_preference(key, value):
    prefs = load_preferences()
    if value is None:
        prefs.pop(key, None)
    else:
        prefs[key] = value
    save_preferences(prefs)


def user_config_path():
    """The single per-user config file, used when no directory or -f is given.

    Resolution: the FMON_CONFIG environment variable, else the location
    recorded in the preferences (see relocate_user_config), else
    <user_config_dir>/fmon.ini.
    """
    env = os.environ.get(CONFIG_ENV, "").strip()
    if env:
        return os.path.abspath(env)
    chosen = load_preferences().get("config_file")
    if chosen:
        return os.path.abspath(chosen)
    return default_user_config_path()


def relocate_user_config(new_path, move=True):
    """Change where the user's config file lives.

    If the current file exists and the new one does not, it is moved there
    (with move=True). The new location is recorded in the preferences so
    both the CLI and the GUI use it from now on. Returns the new path.
    """
    new_path = os.path.abspath(new_path)
    current = user_config_path()
    if os.path.isdir(new_path) or not os.path.splitext(new_path)[1]:
        new_path = os.path.join(new_path, CONFIG_FILENAME)   # a directory: fmon.ini inside it
    if _canonical(new_path) != _canonical(current):
        not_permitted = ConfigPermissionError(
            f"you are not permitted to create {new_path} (check the directory's permissions)"
        )
        if not is_writable(new_path):
            raise not_permitted
        if not os.path.exists(new_path):
            # really write there: os.access cannot see ACLs / read-only folders on every platform
            try:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                if move and os.path.isfile(current):
                    shutil.move(current, new_path)
                else:
                    with open(new_path, "w", encoding="utf-8") as f:
                        f.write(CONFIG_TEMPLATE)
            except PermissionError:
                raise not_permitted from None
            except OSError as e:
                raise ConfigError(f"cannot create {new_path}: {e}") from None
    try:
        set_preference("config_file", None if _canonical(new_path) == _canonical(default_user_config_path()) else new_path)
    except OSError as e:
        raise ConfigError(f"cannot save preferences: {e}") from None
    return new_path


def _canonical(path):
    """Canonical form of a path for conflict detection: absolute, symlinks
    resolved, case-normalised (so C:\\A and c:\\a collide on Windows)."""
    return os.path.normcase(os.path.realpath(path))


# --- reading ----------------------------------------------------------------

def _new_parser():
    return configparser.ConfigParser(interpolation=None)


def read_parser(ini_path):
    """Parse an .ini file into a ConfigParser (no semantic validation)."""
    ini_path = os.path.abspath(ini_path)
    if not os.path.isfile(ini_path):
        raise ConfigError(f"Config file not found: {ini_path}")
    parser = _new_parser()
    try:
        with open(ini_path, "r", encoding="utf-8") as f:
            parser.read_file(f)
    except configparser.DuplicateSectionError as e:
        raise ConfigError(f"{ini_path}: duplicate target name [{e.section}]") from None
    except configparser.Error as e:
        raise ConfigError(f"{ini_path}: {e}") from None
    except PermissionError:
        raise ConfigPermissionError(f"you are not permitted to read {ini_path}") from None
    return parser


def read_text(ini_path):
    """Raw text of the config file (for display)."""
    ini_path = os.path.abspath(ini_path)
    if not os.path.isfile(ini_path):
        raise ConfigError(f"Config file not found: {ini_path}")
    try:
        with open(ini_path, "r", encoding="utf-8") as f:
            return f.read()
    except PermissionError:
        raise ConfigPermissionError(f"you are not permitted to read {ini_path}") from None


def validate(parser, ini_path, allow_empty=False):
    """Semantically validate a parsed config and return its Targets in order."""
    ini_path = os.path.abspath(ini_path)
    ini_dir = os.path.dirname(ini_path)
    control_dir = ini_dir
    if parser.has_section(GLOBAL_SECTION):
        control_dir = parser.get(GLOBAL_SECTION, "control_dir", fallback=ini_dir)
        control_dir = os.path.abspath(os.path.join(ini_dir, control_dir))
        unknown = set(parser.options(GLOBAL_SECTION)) - set(GLOBAL_OPTIONS) - set(DEPRECATED_OPTIONS)
        if unknown:
            raise ConfigError(
                f"{ini_path}: unknown option(s) in [{GLOBAL_SECTION}]: {', '.join(sorted(unknown))}"
            )
        _log_settings_from_parser(parser, ini_path)  # raises on bad values

    targets = []
    seen_controls = {}
    for name in parser.sections():
        if name == GLOBAL_SECTION:
            continue
        if not _NAME_RE.match(name):
            raise ConfigError(
                f"{ini_path}: invalid target name [{name}] "
                "(use letters, digits, '.', '_' or '-')"
            )
        section = parser[name]

        unknown = set(section.keys()) - set(TARGET_OPTIONS)
        if unknown:
            raise ConfigError(
                f"{ini_path}: unknown option(s) in [{name}]: {', '.join(sorted(unknown))}"
            )

        raw_path = section.get("path", "").strip()
        if not raw_path:
            raise ConfigError(f"{ini_path}: target [{name}] is missing required option 'path'")
        path = os.path.abspath(os.path.join(ini_dir, raw_path))

        raw_control = section.get("control_file", "").strip() or name + CONTROL_EXT
        control_file = os.path.abspath(os.path.join(control_dir, raw_control))

        try:
            schedule = parse_interval(section.get("schedule", ""))
        except ValueError as e:
            raise ConfigError(f"{ini_path}: target [{name}] schedule: {e}") from None

        key = _canonical(control_file)
        if key in seen_controls:
            raise ConfigError(
                f"{ini_path}: targets [{seen_controls[key]}] and [{name}] "
                f"use the same control file: {control_file}"
            )
        seen_controls[key] = name

        targets.append(Target(name=name, path=path, control_file=control_file, schedule=schedule))

    if not targets and not allow_empty:
        raise ConfigError(f"{ini_path}: no targets defined")
    return targets


def load_config(ini_path, allow_empty=False):
    """Parse an .ini file and return a list of validated Targets, in file order."""
    return validate(read_parser(ini_path), ini_path, allow_empty=allow_empty)


def config_warnings(ini_path):
    """Non-fatal problems of a config file (e.g. obsolete options): list of
    (section, option, message). Empty if the file is missing or unreadable."""
    ini_path = os.path.abspath(ini_path)
    if not os.path.isfile(ini_path):
        return []
    try:
        parser = read_parser(ini_path)
    except ConfigError:
        return []
    found = []
    if parser.has_section(GLOBAL_SECTION):
        for option in parser.options(GLOBAL_SECTION):
            if option in DEPRECATED_OPTIONS:
                found.append((GLOBAL_SECTION, option, DEPRECATED_OPTIONS[option]))
    return found


def format_size(size):
    """Bytes -> '1M', '512K', '2G' when exact, else the number of bytes."""
    for unit, factor in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if size and size % factor == 0:
            return f"{size // factor}{unit}"
    return str(size)


# --- comment-preserving editing ----------------------------------------------

_SECTION_RE = re.compile(r"^\s*\[(?P<name>.+)\]")  # same header rule as configparser
_OPTION_RE = re.compile(r"^(?P<key>[^\s;#=:][^=:]*?)\s*[=:]")
_COMMENT_RE = re.compile(r"^\s*[;#]")


class IniText:
    """Minimal line-based .ini editor that keeps comments and layout intact.

    configparser is still used for parsing/validation; this class only
    performs the textual edits so that hand-written comments survive.
    """

    def __init__(self, text=""):
        self.lines = text.splitlines()

    @property
    def text(self):
        return "\n".join(self.lines) + ("\n" if self.lines else "")

    def _sections(self):
        """[(name, header_index, end_index_exclusive), ...] in file order."""
        found = []
        for i, line in enumerate(self.lines):
            m = _SECTION_RE.match(line)
            if m and not _COMMENT_RE.match(line):
                found.append([m.group("name"), i, None])
        for idx, sec in enumerate(found):
            sec[2] = found[idx + 1][1] if idx + 1 < len(found) else len(self.lines)
        return [tuple(sec) for sec in found]

    def _find(self, section):
        for name, start, end in self._sections():
            if name == section:
                return start, end
        return None

    def has_section(self, section):
        return self._find(section) is not None

    def section_text(self, section):
        rng = self._find(section)
        if rng is None:
            return None
        start, end = rng
        lines = self.lines[start:end]
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines) + "\n"

    def _option_index(self, section, option):
        rng = self._find(section)
        if rng is None:
            return None
        start, end = rng
        for i in range(start + 1, end):
            line = self.lines[i]
            if _COMMENT_RE.match(line):
                continue
            m = _OPTION_RE.match(line)
            if m and m.group("key").strip().lower() == option.lower():
                return i
        return None

    def has_option(self, section, option):
        return self._option_index(section, option) is not None

    def add_section(self, section):
        if self.has_section(section):
            return
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(f"[{section}]")

    def remove_section(self, section):
        rng = self._find(section)
        if rng is None:
            return False
        start, end = rng
        del self.lines[start:end]
        # avoid leaving a double blank line behind
        while start < len(self.lines) and start > 0 and not self.lines[start].strip() \
                and not self.lines[start - 1].strip():
            del self.lines[start]
        return True

    def set(self, section, option, value):
        self.add_section(section)
        idx = self._option_index(section, option)
        new_line = f"{option} = {value}"
        if idx is not None:
            self.lines[idx] = new_line
            return
        start, end = self._find(section)
        insert_at = end
        while insert_at > start + 1 and not self.lines[insert_at - 1].strip():
            insert_at -= 1
        self.lines.insert(insert_at, new_line)

    def remove_option(self, section, option):
        idx = self._option_index(section, option)
        if idx is None:
            return False
        del self.lines[idx]
        return True


def _parse_text(text, ini_path):
    parser = _new_parser()
    try:
        parser.read_string(text, source=ini_path)
    except configparser.DuplicateSectionError as e:
        raise ConfigError(f"{ini_path}: duplicate target name [{e.section}]") from None
    except configparser.Error as e:
        raise ConfigError(f"{ini_path}: {e}") from None
    return parser


# --- writing ----------------------------------------------------------------

def is_writable(ini_path):
    """True if the current user may modify the file (or create it)."""
    ini_path = os.path.abspath(ini_path)
    if os.path.exists(ini_path):
        return os.access(ini_path, os.W_OK)
    parent = os.path.dirname(ini_path) or "."
    while parent and not os.path.exists(parent):
        parent = os.path.dirname(parent)
    return os.access(parent or ".", os.W_OK)


def _not_permitted(ini_path):
    return ConfigPermissionError(f"you are not permitted to modify {ini_path} (read-only)")


def write_text(ini_path, text):
    """Write the config file in place.

    Deliberately writes the existing file directly (no temp-file + rename),
    so a read-only file stays protected even if its directory is writable.
    """
    ini_path = os.path.abspath(ini_path)
    try:
        os.makedirs(os.path.dirname(ini_path), exist_ok=True)
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(text)
    except PermissionError:
        raise _not_permitted(ini_path) from None


def _edit(ini_path, mutate, create=False):
    """Read, apply mutate(IniText), validate the result, then write.

    Nothing is written if validation fails, and a permission check is done
    up front so users with a read-only config get a clear message. With
    create=True a missing file is created from the template.
    """
    ini_path = os.path.abspath(ini_path)
    if os.path.isfile(ini_path):
        text = read_text(ini_path)
    elif create:
        text = CONFIG_TEMPLATE
    else:
        raise ConfigError(f"Config file not found: {ini_path}")
    if not is_writable(ini_path):
        raise _not_permitted(ini_path)
    ini = IniText(text)
    mutate(ini)
    targets = validate(_parse_text(ini.text, ini_path), ini_path, allow_empty=True)
    write_text(ini_path, ini.text)
    return targets


def set_option(ini_path, section, option, value):
    """Set (add or change) an option; the section - and a missing config
    file - are created if needed."""
    option = option.strip().lower()
    allowed = GLOBAL_OPTIONS if section == GLOBAL_SECTION else TARGET_OPTIONS
    if option not in allowed:
        raise ConfigError(
            f"unknown option '{option}' for [{section}] (allowed: {', '.join(allowed)})"
        )
    return _edit(ini_path, lambda ini: ini.set(section, option, value), create=True)


def unset_option(ini_path, section, option):
    """Remove an option from a section (including obsolete ones)."""
    option = option.strip().lower()

    def mutate(ini):
        if not ini.has_section(section):
            raise ConfigError(f"no section [{section}] in {ini_path}")
        if not ini.remove_option(section, option):
            raise ConfigError(f"no option '{option}' in [{section}]")

    return _edit(ini_path, mutate)


def add_target(ini_path, name, path, control_file=None):
    """Add a new monitored folder."""
    def mutate(ini):
        if ini.has_section(name):
            raise ConfigError(f"target [{name}] already exists (use 'config set' to change it)")
        ini.add_section(name)
        ini.set(name, "path", path)
        if control_file:
            ini.set(name, "control_file", control_file)

    return _edit(ini_path, mutate, create=True)


def remove_target(ini_path, name):
    """Remove a monitored folder (its control file is left untouched)."""
    def mutate(ini):
        if name == GLOBAL_SECTION or not ini.has_section(name):
            raise ConfigError(f"no target [{name}] in {ini_path}")
        ini.remove_section(name)

    return _edit(ini_path, mutate)


# --- registration of ad-hoc control files -------------------------------------

def suggest_name(path):
    """Derive a valid target name from a folder path (its base name)."""
    base = os.path.basename(os.path.abspath(path).rstrip("/\\"))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", base).lstrip("._-")
    return name or "target"


def register_target(ini_path, path, control_file, name=None):
    """Record a folder/control-file pair in the config, creating the file if
    needed. Returns (Target, action, undo) with action in 'added',
    'updated', 'unchanged' and undo a callable that reverts the change
    (None when nothing changed).

    * A target already using this control file for this folder is reused.
    * Using an existing control file for a *different* folder is an error.
    * A target with the same name watching the same folder is updated to
      the new control file; if it watches another folder, a numeric suffix
      is appended to the name.
    """
    ini_path = os.path.abspath(ini_path)
    path = os.path.abspath(path)
    control_file = os.path.abspath(control_file)
    existing = load_config(ini_path, allow_empty=True) if os.path.isfile(ini_path) else []

    if name is not None:
        if not _NAME_RE.match(name):
            raise ConfigError(f"invalid target name '{name}' (use letters, digits, '.', '_' or '-')")
        same = next((t for t in existing if t.name == name), None)
        if same and _canonical(same.path) != _canonical(path):
            raise ConfigError(f"target [{name}] already watches a different folder: {same.path}")

    for t in existing:
        if _canonical(t.control_file) == _canonical(control_file):
            if _canonical(t.path) == _canonical(path):
                return t, "unchanged", None
            raise ConfigError(
                f"control file {control_file} is already used by target [{t.name}] "
                f"for folder {t.path}"
            )

    if name is not None:
        chosen = name
    else:
        base = suggest_name(path)
        chosen = base
        by_name = {t.name: t for t in existing}
        n = 2
        while chosen in by_name and _canonical(by_name[chosen].path) != _canonical(path):
            chosen = f"{base}-{n}"
            n += 1

    previous = next((t for t in existing if t.name == chosen), None)
    action = "updated" if previous else "added"

    def mutate(ini):
        ini.set(chosen, "path", path)
        ini.set(chosen, "control_file", control_file)

    targets = _edit(ini_path, mutate, create=True)

    def undo():
        if previous is None:
            remove_target(ini_path, chosen)
        else:
            set_option(ini_path, chosen, "control_file", previous.control_file)

    return next(t for t in targets if t.name == chosen), action, undo


# --- lookup -------------------------------------------------------------------

def is_pattern(name):
    return any(c in _GLOB_CHARS for c in name)


def match_targets(targets, pattern):
    """Targets whose name equals `pattern` or matches it as a glob ('t*')."""
    if is_pattern(pattern):
        return [t for t in targets if fnmatch.fnmatchcase(t.name, pattern)]
    return [t for t in targets if t.name == pattern]
