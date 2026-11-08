"""Audit log of fmon commands, with size-based rotation.

One entry per command. The first line has the fixed structure

    TYPE | TIMESTAMP | USER | COMMAND | RESULT

and may be followed by indented detail lines (e.g. the full check report,
so the log shows exactly which files were added, removed or modified):

    Info    | 2026-11-06 23:02:02 | kehsi | fmon -c test2 | C:\\...\\test2: 1 added, 0 removed, 0 modified
        fmon check: C:\\...\\test2
        Control file: C:\\...\\test2.ctl
        Added: 1
          another.txt  (size 34 B, perms 0666 (rw-rw-rw-))
        Removed: 0
        Modified: 0

    Info    - the command succeeded
    Warning - the user attempted an action reserved to administrators
              (e.g. modifying a read-only configuration)
    Error   - the command failed: permission errors while writing, broken
              command lines, missing folders or control files, ...

When the log exceeds its maximum size it is renamed to
    <name>_<first entry time>_to_<last entry time>.log
e.g. fmon_2026-11-06-21-00-05_to_2026-11-06-23-00-10.log. Rotated logs
are kept forever (keep_days = 0) or deleted once they are older than
keep_days days.
"""
import getpass
import glob
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta

INFO = "Info"
WARNING = "Warning"
ERROR = "Error"
LEVELS = (INFO, WARNING, ERROR)

SEPARATOR = " | "
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_TIME_FORMAT = "%Y-%m-%d-%H-%M-%S"
INDENT = "    "

_ENTRY_RE = re.compile(
    r"^(?P<level>" + "|".join(LEVELS) + r")\s*\| (?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| "
)


def current_user():
    try:
        return getpass.getuser()
    except Exception:  # no controlling user (e.g. some service contexts)
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def format_command(argv, prog="fmon"):
    """Render the command line as the user typed it (quoted where needed)."""
    parts = [prog] + list(argv)
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _clean(text):
    """Collapse text to a single line."""
    return " / ".join(part.strip() for part in str(text).splitlines() if part.strip())


def format_entry(level, command, result, user=None, when=None, details=None):
    """One log entry: the header line plus indented detail lines."""
    if level not in LEVELS:
        raise ValueError(f"unknown log level {level!r}")
    when = when or datetime.now()
    user = user or current_user()
    lines = [SEPARATOR.join([
        f"{level:<7}",
        when.strftime(TIME_FORMAT),
        _clean(user),
        _clean(command),
        _clean(result) or "-",
    ])]
    for line in (details or "").splitlines():
        if line.strip():
            lines.append(INDENT + line.rstrip())
    return "\n".join(lines) + "\n"


def is_entry_start(line):
    return _ENTRY_RE.match(line) is not None


def parse_entries(text):
    """Split log text into entries (each a list of lines, header first)."""
    entries = []
    for line in text.splitlines():
        if is_entry_start(line) or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    return entries


def entry_time_span(path):
    """(first, last) timestamps of the entries in a log file, or None."""
    first = last = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _ENTRY_RE.match(line)
            if m:
                ts = datetime.strptime(m.group("time"), TIME_FORMAT)
                first = first or ts
                last = ts
    return first, last


def _split_name(path):
    directory, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    return directory, stem, ext


def rotated_name(path, first, last):
    """Name for the rotated copy of `path` covering first..last."""
    directory, stem, ext = _split_name(path)
    base = f"{stem}_{first.strftime(FILE_TIME_FORMAT)}_to_{last.strftime(FILE_TIME_FORMAT)}"
    candidate = os.path.join(directory, base + ext)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{n}{ext}")
        n += 1
    return candidate


def rotated_files(path):
    """Rotated copies of `path`, oldest first (the names sort by time)."""
    directory, stem, ext = _split_name(path)
    pattern = os.path.join(glob.escape(directory), glob.escape(stem) + "_*_to_*" + glob.escape(ext))
    return sorted(glob.glob(pattern))


_ROTATED_RE = re.compile(r"_(\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d)_to_(\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d)(?:_\d+)?$")


def rotated_file_span(path):
    """(first, last) datetimes encoded in a rotated file's name, or the
    file's modification time for both if the name does not carry them."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = _ROTATED_RE.search(stem)
    if m:
        try:
            return (datetime.strptime(m.group(1), FILE_TIME_FORMAT),
                    datetime.strptime(m.group(2), FILE_TIME_FORMAT))
        except ValueError:
            pass
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return mtime, mtime


def prune(path, keep_days, now=None):
    """Delete rotated logs whose last entry is older than keep_days days.
    keep_days == 0 keeps everything. Returns the deleted paths."""
    if keep_days <= 0:
        return []
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    deleted = []
    for old in rotated_files(path):
        _, last = rotated_file_span(old)
        if last < cutoff:
            os.remove(old)
            deleted.append(old)
    return deleted


def rotate(path, keep_days=0):
    """Rename the log to <name>_<first>_to_<last>.log, then delete rotated
    logs older than keep_days days (0 = never delete). Returns the new name."""
    first, last = entry_time_span(path)
    if first is None:
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        first = last = mtime
    target = rotated_name(path, first, last)
    os.replace(path, target)
    prune(path, keep_days)
    return target


def write_entry(settings, level, command, result, user=None, when=None, details=None):
    """Append one entry, rotating first if the file would exceed max_size.

    Never raises: a log that cannot be written must not break the command,
    but the problem is reported on stderr so it does not go unnoticed.
    Returns True if the entry was written.
    """
    if settings is None or not settings.path:
        return False
    text = format_entry(level, command, result, user=user, when=when, details=details)
    path = settings.path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if settings.max_size > 0 and os.path.exists(path):
            if os.path.getsize(path) + len(text.encode("utf-8")) > settings.max_size:
                try:
                    rotate(path, settings.keep_days)
                except OSError as e:
                    print(f"warning: could not rotate log file {path}: {e}", file=sys.stderr)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError as e:
        print(f"warning: could not write log file {path}: {e}", file=sys.stderr)
        return False


def tail(path, count):
    """Last `count` entries of the log as text blocks (all if count <= 0)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        entries = parse_entries(f.read())
    if count > 0:
        entries = entries[-count:]
    return ["\n".join(lines) for lines in entries]
