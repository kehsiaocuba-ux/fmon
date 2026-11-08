"""Reading, filtering and paging of the audit log (used by the GUI).

    entries = load_entries(log_path, include_rotated=True)
    entries = filter_entries(entries, level="Error", text="test1")
    pager = Pager(entries, page_size=25)
    pager.page_entries()   -> entries of the current page
    pager.label()          -> "1 - 25 of 400"
"""
import os
from dataclasses import dataclass, field
from datetime import datetime

from fmon import log as fmon_log


@dataclass
class LogEntry:
    level: str
    time: datetime
    user: str
    command: str
    result: str
    details: list = field(default_factory=list)   # indented report lines (dedented)
    source: str = ""                               # file the entry came from

    @property
    def text(self):
        """The entry as it appears in the log file."""
        header = fmon_log.SEPARATOR.join([
            f"{self.level:<7}", self.time.strftime(fmon_log.TIME_FORMAT),
            self.user, self.command, self.result,
        ])
        return "\n".join([header] + [fmon_log.INDENT + d for d in self.details])

    def matches(self, text):
        needle = text.lower()
        return needle in self.text.lower()


def parse_entry(lines, source=""):
    """Parse one entry (header line + detail lines); None if malformed."""
    header = lines[0]
    parts = header.split(fmon_log.SEPARATOR, 4)
    if len(parts) != 5 or not fmon_log.is_entry_start(header):
        return None
    level, when, user, command, result = (p.strip() for p in parts)
    try:
        time = datetime.strptime(when, fmon_log.TIME_FORMAT)
    except ValueError:
        return None
    details = []
    for line in lines[1:]:
        if line.startswith(fmon_log.INDENT):
            details.append(line[len(fmon_log.INDENT):])
        elif line.strip():
            details.append(line.strip())
    return LogEntry(level=level, time=time, user=user, command=command,
                    result=result, details=details, source=source)


def load_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    entries = []
    for lines in fmon_log.parse_entries(text):
        entry = parse_entry(lines, source=os.path.basename(path))
        if entry is not None:
            entries.append(entry)
    return entries


def load_entries(log_path, include_rotated=False):
    """All entries, oldest first. With include_rotated, the rotated files
    (oldest first) come before the current log."""
    if not log_path:
        return []
    files = fmon_log.rotated_files(log_path) if include_rotated else []
    files.append(log_path)
    entries = []
    for path in files:
        if os.path.isfile(path):
            entries.extend(load_file(path))
    return entries


def users(entries):
    return sorted({e.user for e in entries})


def filter_entries(entries, level=None, user=None, text=None, since=None, until=None,
                   command=None):
    """Keep the entries matching every given criterion.

    level   - "Info" | "Warning" | "Error" (None/"All" = any)
    user    - exact user name
    text    - case-insensitive substring anywhere in the entry (incl. report)
    command - case-insensitive substring of the command line
    since/until - datetimes (inclusive)
    """
    out = []
    for e in entries:
        if level and level != "All" and e.level != level:
            continue
        if user and user != "All" and e.user != user:
            continue
        if since and e.time < since:
            continue
        if until and e.time > until:
            continue
        if command and command.lower() not in e.command.lower():
            continue
        if text and not e.matches(text):
            continue
        out.append(e)
    return out


def parse_date(text, end_of_day=False):
    """'2026-11-06' or '2026-11-06 23:00' -> datetime; '' -> None."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            value = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                value = value.replace(hour=23, minute=59, second=59)
            return value
        except ValueError:
            continue
    raise ValueError(f"invalid date '{text}' (use YYYY-MM-DD or YYYY-MM-DD HH:MM)")


PAGE_SIZES = (25, 50, 100)


class Pager:
    """Fixed-size pages over a list, newest first by default."""

    def __init__(self, entries, page_size=25, newest_first=True):
        self.entries = list(reversed(entries)) if newest_first else list(entries)
        self.page_size = max(1, int(page_size))
        self.page = 1

    @property
    def total(self):
        return len(self.entries)

    @property
    def pages(self):
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    def set_page_size(self, size):
        first_index = (self.page - 1) * self.page_size   # keep roughly the same position
        self.page_size = max(1, int(size))
        self.page = first_index // self.page_size + 1

    def goto(self, page):
        self.page = min(max(1, int(page)), self.pages)
        return self.page

    def first(self):
        return self.goto(1)

    def last(self):
        return self.goto(self.pages)

    def next(self):
        return self.goto(self.page + 1)

    def prev(self):
        return self.goto(self.page - 1)

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def has_prev(self):
        return self.page > 1

    def page_entries(self):
        start = (self.page - 1) * self.page_size
        return self.entries[start:start + self.page_size]

    def label(self):
        if self.total == 0:
            return "0 of 0"
        start = (self.page - 1) * self.page_size + 1
        end = min(start + self.page_size - 1, self.total)
        return f"{start} - {end} of {self.total}"
