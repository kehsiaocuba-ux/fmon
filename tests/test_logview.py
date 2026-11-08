from datetime import datetime
import pytest
from fmon import logview
from fmon import log as fmon_log
from fmon.config import LogSettings


def make_log(path, count, start=datetime(2026, 11, 1, 8, 0, 0)):
    settings = LogSettings(path=str(path), max_size=0, keep_days=0)
    for i in range(count):
        level = ("Info", "Warning", "Error")[i % 3]
        user = ("alice", "bob")[i % 2]
        when = start.replace(day=1 + i // 24, hour=i % 24)
        details = f"fmon check: /x\nAdded: 1\n  file{i}.txt  (size 1 B)" if level == "Info" else None
        fmon_log.write_entry(settings, level, f"fmon cmd{i}", f"result {i}", user=user, when=when, details=details)
    return path


def test_parse_entry_roundtrip():
    lines = ["Info    | 2026-11-07 01:02:03 | kehsi | fmon -c test2 | 1 added",
             "    fmon check: /x", "    Added: 1", "      another.txt  (size 34 B)"]
    e = logview.parse_entry(lines, source="fmon.log")
    assert (e.level, e.user, e.command, e.result) == ("Info", "kehsi", "fmon -c test2", "1 added")
    assert e.time == datetime(2026, 11, 7, 1, 2, 3)
    assert e.details == ["fmon check: /x", "Added: 1", "  another.txt  (size 34 B)"]
    assert e.text == "\n".join(lines)
    assert e.matches("ANOTHER.txt") and not e.matches("zzz")
    assert logview.parse_entry(["garbage line"]) is None


def test_load_entries_and_rotated(tmp_path):
    log = tmp_path / "fmon.log"
    make_log(log, 5)
    old = tmp_path / "fmon_2026-10-01-00-00-00_to_2026-10-02-00-00-00.log"
    old.write_text("Error   | 2026-10-01 00:00:00 | carol | fmon old | failed\n")
    entries = logview.load_entries(str(log))
    assert len(entries) == 5 and entries[0].command == "fmon cmd0"
    entries = logview.load_entries(str(log), include_rotated=True)
    assert len(entries) == 6 and entries[0].user == "carol" and entries[0].source.startswith("fmon_2026")
    assert logview.load_entries(None) == []
    assert logview.load_entries(str(tmp_path / "missing.log")) == []
    assert logview.users(entries) == ["alice", "bob", "carol"]


def test_filter_entries(tmp_path):
    entries = logview.load_entries(str(make_log(tmp_path / "fmon.log", 30)))
    assert len(logview.filter_entries(entries, level="Error")) == 10
    assert len(logview.filter_entries(entries, level="All")) == 30
    assert len(logview.filter_entries(entries, user="alice")) == 15
    assert len(logview.filter_entries(entries, level="Warning", user="bob")) == 5
    assert [e.command for e in logview.filter_entries(entries, command="cmd1")] == [f"fmon cmd{i}" for i in (1, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19)]
    assert len(logview.filter_entries(entries, text="file3.txt")) == 1   # searches the report
    assert len(logview.filter_entries(entries, text="FILE")) == 10        # case-insensitive, Info only
    day2 = logview.filter_entries(entries, since=datetime(2026, 11, 2))
    assert len(day2) == 6 and all(e.time.day == 2 for e in day2)
    window = logview.filter_entries(entries, since=datetime(2026, 11, 1, 10), until=datetime(2026, 11, 1, 12))
    assert [e.time.hour for e in window] == [10, 11, 12]


def test_parse_date():
    assert logview.parse_date("") is None
    assert logview.parse_date("2026-11-06") == datetime(2026, 11, 6)
    assert logview.parse_date("2026-11-06", end_of_day=True) == datetime(2026, 11, 6, 23, 59, 59)
    assert logview.parse_date("2026-11-06 23:00") == datetime(2026, 11, 6, 23, 0)
    with pytest.raises(ValueError):
        logview.parse_date("yesterday")


def test_pager_labels_and_navigation():
    pager = logview.Pager(list(range(400)), 25)
    assert pager.total == 400 and pager.pages == 16
    assert pager.label() == "1 - 25 of 400"
    assert pager.page_entries()[0] == 399          # newest first
    assert not pager.has_prev and pager.has_next
    pager.next()
    assert pager.label() == "26 - 50 of 400"
    pager.last()
    assert pager.page == 16 and pager.label() == "376 - 400 of 400" and not pager.has_next
    pager.next()                                   # clamped
    assert pager.page == 16
    pager.goto(0)
    assert pager.page == 1
    pager.goto(7)
    pager.set_page_size(50)                        # keeps position: entry 151 is on page 4 of 50
    assert pager.page == 4 and pager.page_size == 50 and pager.pages == 8
    assert pager.label() == "151 - 200 of 400"
    pager.set_page_size(100)
    assert pager.pages == 4 and pager.label() == "101 - 200 of 400"


def test_pager_partial_last_page_and_empty():
    pager = logview.Pager(list(range(53)), 25)
    pager.last()
    assert pager.label() == "51 - 53 of 53" and len(pager.page_entries()) == 3
    empty = logview.Pager([], 25)
    assert empty.pages == 1 and empty.label() == "0 of 0" and empty.page_entries() == []
    assert not empty.has_next and not empty.has_prev
