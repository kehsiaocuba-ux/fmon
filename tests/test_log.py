import os
import re
from datetime import datetime, timedelta
import pytest
from fmon import log as fmon_log
from fmon import config as fmon_config
from fmon.config import LogSettings, parse_size, log_settings, load_config, ConfigError
from fmon import cli


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    path = tmp_path / "userhome" / "fmon" / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(path))
    return path


@pytest.fixture
def user_log(isolated_user_config):
    return isolated_user_config.parent / "fmon.log"


def entries(log_path):
    """Header lines of the entries in a log file."""
    return [l for l in log_path.read_text().splitlines() if fmon_log.is_entry_start(l)]


def blocks(log_path):
    return fmon_log.parse_entries(log_path.read_text())


def fields(line):
    return [f.strip() for f in line.split(" | ")]


# --- formatting --------------------------------------------------------------

def test_format_entry_structure():
    when = datetime(2026, 11, 7, 1, 2, 3)
    line = fmon_log.format_entry("Info", "fmon test1", "snapshot saved", user="alice", when=when)
    assert line == "Info    | 2026-11-07 01:02:03 | alice | fmon test1 | snapshot saved\n"
    assert fields(line) == ["Info", "2026-11-07 01:02:03", "alice", "fmon test1", "snapshot saved"]


def test_format_entry_single_line_and_defaults():
    line = fmon_log.format_entry("Error", "fmon x", "first line\n  second line\n", user="u")
    assert line.count("\n") == 1
    assert "first line / second line" in line
    line = fmon_log.format_entry("Warning", "fmon x", "", user="u")
    assert line.rstrip("\n").endswith(" | -")
    with pytest.raises(ValueError):
        fmon_log.format_entry("Debug", "fmon", "x", user="u")


def test_format_entry_with_details():
    when = datetime(2026, 11, 7, 1, 2, 3)
    text = fmon_log.format_entry("Info", "fmon -c test2", "1 added", user="alice", when=when,
                                 details="fmon check: /x\n\nAdded: 1\n  another.txt  (size 34 B)\n")
    assert text == (
        "Info    | 2026-11-07 01:02:03 | alice | fmon -c test2 | 1 added\n"
        "    fmon check: /x\n"
        "    Added: 1\n"
        "      another.txt  (size 34 B)\n"
    )
    (entry,) = fmon_log.parse_entries(text)
    assert len(entry) == 4 and fmon_log.is_entry_start(entry[0])
    assert not any(fmon_log.is_entry_start(l) for l in entry[1:])


def test_tail_returns_whole_entries(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=0, keep_days=0)
    fmon_log.write_entry(settings, "Info", "fmon a", "one", user="u", details="d1\nd2")
    fmon_log.write_entry(settings, "Error", "fmon b", "two", user="u")
    fmon_log.write_entry(settings, "Info", "fmon c", "three", user="u", details="d3")
    last2 = fmon_log.tail(str(path), 2)
    assert len(last2) == 2
    assert last2[0].startswith("Error") and "\n" not in last2[0]
    assert last2[1].startswith("Info") and last2[1].endswith("\n    d3")
    assert len(fmon_log.tail(str(path), 0)) == 3


def test_format_command_quotes_arguments():
    cmd = fmon_log.format_command(["config", "show", "t*", "my dir"])
    assert cmd.startswith("fmon config show ")
    assert "my dir" in cmd and ('"my dir"' in cmd or "'my dir'" in cmd)


def test_current_user_is_nonempty():
    assert fmon_log.current_user()


# --- rotation ----------------------------------------------------------------

def test_write_entry_appends(tmp_path):
    settings = LogSettings(path=str(tmp_path / "fmon.log"), max_size=0, keep_days=3)
    assert fmon_log.write_entry(settings, "Info", "fmon a", "one", user="u")
    assert fmon_log.write_entry(settings, "Error", "fmon b", "two", user="u")
    lines = entries(tmp_path / "fmon.log")
    assert len(lines) == 2
    assert fields(lines[0])[0] == "Info" and fields(lines[1])[0] == "Error"


def test_write_entry_creates_parent_dir(tmp_path):
    settings = LogSettings(path=str(tmp_path / "deep" / "er" / "fmon.log"), max_size=0, keep_days=1)
    assert fmon_log.write_entry(settings, "Info", "fmon", "x", user="u")
    assert (tmp_path / "deep" / "er" / "fmon.log").exists()


def _write_at(settings, when, text):
    fmon_log.write_entry(settings, "Info", "fmon", text, user="u", when=when)


def test_rotation_names_files_by_time_span(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=150, keep_days=0)  # ~2 entries per file
    t = datetime(2026, 11, 6, 21, 0, 5)
    _write_at(settings, t, "entry 0")
    _write_at(settings, t.replace(hour=22, minute=30, second=0), "entry 1")
    _write_at(settings, t.replace(hour=23, second=10), "entry 2")  # triggers rotation
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["fmon.log", "fmon_2026-11-06-21-00-05_to_2026-11-06-22-30-00.log"]
    rotated = tmp_path / names[1]
    assert "entry 0" in rotated.read_text() and "entry 1" in rotated.read_text()
    assert entries(path) and "entry 2" in path.read_text()


def test_rotated_logs_kept_forever_by_default(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=80, keep_days=0)  # one entry per file
    base = datetime(2020, 1, 1, 0, 0, 0)                               # long ago
    for i in range(5):
        _write_at(settings, base.replace(hour=i), f"entry {i}")
    assert len(fmon_log.rotated_files(str(path))) == 4                 # nothing deleted


def test_prune_deletes_rotated_logs_older_than_keep_days(tmp_path):
    path = tmp_path / "fmon.log"
    now = datetime(2026, 3, 1, 12, 0, 0)
    names = []
    for days_ago in (100, 61, 59, 1):
        t = now - timedelta(days=days_ago)
        name = f"fmon_{(t - timedelta(hours=1)).strftime(fmon_log.FILE_TIME_FORMAT)}_to_{t.strftime(fmon_log.FILE_TIME_FORMAT)}.log"
        (tmp_path / name).write_text("x")
        names.append(name)
    unnamed = tmp_path / "fmon_broken_to_name.log"                     # falls back to mtime (now) -> kept
    unnamed.write_text("x")
    deleted = fmon_log.prune(str(path), keep_days=60, now=now)
    assert sorted(os.path.basename(d) for d in deleted) == sorted(names[:2])
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert names[2] in remaining and names[3] in remaining and unnamed.name in remaining
    assert fmon_log.prune(str(path), keep_days=0, now=now) == []        # 0 = never delete


def test_rotation_prunes_by_keep_days(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=80, keep_days=30)
    t = datetime.now() - timedelta(days=45)
    stale = tmp_path / f"fmon_{t.strftime(fmon_log.FILE_TIME_FORMAT)}_to_{t.strftime(fmon_log.FILE_TIME_FORMAT)}.log"
    stale.write_text("ancient")
    recent = datetime.now() - timedelta(days=2)
    _write_at(settings, recent, "entry 0")
    _write_at(settings, recent + timedelta(hours=1), "entry 1")     # rotates entry 0 (2 days old: kept), prunes the stale file
    rotated = [os.path.basename(r) for r in fmon_log.rotated_files(str(path))]
    assert not stale.exists()
    assert len(rotated) == 1 and "entry 0" in (tmp_path / rotated[0]).read_text()


def test_rotation_name_collision_gets_suffix(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=80, keep_days=0)
    t = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):  # all in the same second
        _write_at(settings, t, f"entry {i}")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [
        "fmon.log",
        "fmon_2026-01-01-12-00-00_to_2026-01-01-12-00-00.log",
        "fmon_2026-01-01-12-00-00_to_2026-01-01-12-00-00_2.log",
    ]


def test_rotation_uses_mtime_when_no_entries(tmp_path):
    path = tmp_path / "fmon.log"
    path.write_text("garbage " * 20)
    target = fmon_log.rotate(str(path), keep_days=3)
    assert os.path.exists(target) and not path.exists()
    assert re.match(r"fmon_\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d_to_\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d\.log$",
                    os.path.basename(target))


def test_no_rotation_when_max_size_zero(tmp_path):
    path = tmp_path / "fmon.log"
    settings = LogSettings(path=str(path), max_size=0, keep_days=2)
    for i in range(20):
        fmon_log.write_entry(settings, "Info", "fmon", f"entry {i}", user="u")
    assert len(entries(path)) == 20


def test_disabled_log_writes_nothing(tmp_path):
    assert not fmon_log.write_entry(LogSettings(path=None, max_size=0, keep_days=0), "Info", "fmon", "x")
    assert not fmon_log.write_entry(None, "Info", "fmon", "x")
    assert list(tmp_path.iterdir()) == []


def test_unwritable_log_does_not_raise(tmp_path, capsys):
    blocker = tmp_path / "notadir"
    blocker.write_text("x")
    settings = LogSettings(path=str(blocker / "fmon.log"), max_size=0, keep_days=0)
    assert not fmon_log.write_entry(settings, "Info", "fmon", "x", user="u")
    assert "could not write log file" in capsys.readouterr().err


def test_tail_counts_entries_not_lines(tmp_path):
    path = tmp_path / "fmon.log"
    h = "Info    | 2026-01-01 00:00:00 | u | fmon x | r"
    path.write_text(f"{h}1\n    detail\n{h}2\n{h}3\n    more\n    more\n")
    assert fmon_log.tail(str(path), 2) == [f"{h}2", f"{h}3\n    more\n    more"]
    assert len(fmon_log.tail(str(path), 0)) == 3


# --- settings ----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("100", 100), ("2K", 2048), ("1M", 1024 ** 2), ("1 mb", 1024 ** 2), ("1G", 1024 ** 3), (512, 512),
])
def test_parse_size(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "1X", "-1", "1.5M"])
def test_parse_size_invalid(value):
    with pytest.raises(ValueError):
        parse_size(value)


def test_log_settings_defaults_when_config_missing(tmp_path):
    s = log_settings(str(tmp_path / "nope.ini"))
    assert s.path == str(tmp_path / "fmon.log")
    assert s.max_size == fmon_config.DEFAULT_LOG_MAX_SIZE
    assert s.keep_days == fmon_config.DEFAULT_LOG_KEEP_DAYS


def test_log_settings_from_config(tmp_path):
    ini = tmp_path / "fmon.ini"
    ini.write_text("[fmon]\nlog_file = logs/audit.log\nlog_max_size = 2K\nlog_keep_days = 9\n")
    s = log_settings(str(ini))
    assert s == LogSettings(path=str(tmp_path / "logs" / "audit.log"), max_size=2048, keep_days=9)

    ini.write_text("[fmon]\nlog_file = off\n")
    assert log_settings(str(ini)).path is None

    ini.write_text("[fmon]\nlog_max_size = lots\n\n[t]\npath = x\n")
    with pytest.raises(ConfigError, match="log_max_size"):
        load_config(str(ini))
    ini.write_text("[fmon]\nlog_keep_days = -1\n\n[t]\npath = x\n")
    with pytest.raises(ConfigError, match="log_keep_days"):
        load_config(str(ini))
    ini.write_text("[fmon]\nlog_backups = 5\n\n[t]\npath = x\n")   # obsolete option: tolerated + reported
    assert [t.name for t in load_config(str(ini))] == ["t"]
    assert log_settings(str(ini)).keep_days == fmon_config.DEFAULT_LOG_KEEP_DAYS
    warnings = fmon_config.config_warnings(str(ini))
    assert len(warnings) == 1 and warnings[0][1] == "log_backups" and "log_keep_days" in warnings[0][2]
    # invalid config still yields usable defaults for logging itself
    assert log_settings(str(ini)).path == str(tmp_path / "fmon.log")


# --- CLI integration ---------------------------------------------------------

def test_every_command_is_logged_with_type(user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "test1").mkdir()
    (tmp_path / "test1" / "a.txt").write_text("x")
    monkeypatch.chdir(tmp_path)

    cli.main(["test1", "-o", "test1.ctl"])                 # INFO: snapshot + registration
    (tmp_path / "test1" / "b.txt").write_text("y")
    with pytest.raises(SystemExit):
        cli.main(["test1", "-c"])                           # INFO: check with changes (exit 1)
    with pytest.raises(SystemExit):
        cli.main(["-c", "nope.ctl"])                        # ERROR: broken command
    with pytest.raises(SystemExit):
        cli.main(["check", "-t", "ghost"])                  # ERROR: unknown target
    with pytest.raises(SystemExit):
        cli.main(["check", "dir", "-t", "usr"])              # ERROR: usage error
    cli.main(["config", "show", "t*"])                      # INFO
    capsys.readouterr()

    lines = entries(user_log)
    assert len(lines) == 6
    rows = [fields(l) for l in lines]
    assert [r[0] for r in rows] == ["Info", "Info", "Error", "Error", "Error", "Info"]
    for r in rows:
        assert len(r) == 5
        datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")       # valid timestamp
        assert r[2] == fmon_log.current_user()
        assert r[3].startswith("fmon ")
    assert rows[0][3] == "fmon test1 -o test1.ctl"
    assert "snapshot of" in rows[0][4] and "target [test1] added" in rows[0][4]
    assert "1 added, 0 removed, 0 modified" in rows[1][4]
    # the full report follows the check entry, so the log tells which file changed
    check_block = blocks(user_log)[1]
    assert check_block[1:] == [
        f"    fmon check: {tmp_path / 'test1'}",
        f"    Control file: {tmp_path / 'test1.ctl'}",
        "    Added: 1",
        "      b.txt  (size 1 B, perms " + fmon_log._clean(check_block[4].split("perms ")[1]),
        "    Removed: 0",
        "    Modified: 0",
    ]
    assert "neither a directory nor an existing control file" in rows[2][4]
    assert "unknown target(s): ghost" in rows[3][4]
    assert "--target requires a config file" in rows[4][4]
    assert "1 target(s) matching t*" in rows[5][4]


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_admin_only_actions_are_warnings(isolated_user_config, user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    monkeypatch.chdir(tmp_path)
    cli.main(["d1"])
    os.chmod(isolated_user_config, 0o444)
    try:
        for argv in (["config", "set", "fmon", "control_dir", "/x"],
                     ["config", "delete", "d1", "--ctl"],
                     ["d2"]):
            with pytest.raises(SystemExit):
                cli.main(argv)
    finally:
        os.chmod(isolated_user_config, 0o644)
    capsys.readouterr()
    rows = [fields(l) for l in entries(user_log)]
    assert [r[0] for r in rows] == ["Info", "Warning", "Warning", "Warning"]
    assert all("not permitted to modify" in r[4] for r in rows[1:])
    assert all("administrator" not in r[4] for r in rows[1:])  # hint is screen-only


def test_write_permission_error_is_error(user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["d1", "-o", str(blocker / "sub" / "d1.ctl")])  # parent is a file
    assert exc.value.code == 2
    assert "cannot write control file" in capsys.readouterr().err
    rows = [fields(l) for l in entries(user_log)]
    assert rows[-1][0] == "Error" and "cannot write control file" in rows[-1][4]
    # the failed snapshot left no entry behind in the config
    assert load_config(str(isolated := fmon_config.user_config_path()), allow_empty=True) == []


def test_failed_snapshot_rolls_back_updated_target(isolated_user_config, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    monkeypatch.chdir(tmp_path)
    cli.main(["d1"])
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit):
        cli.main(["d1", "-o", str(blocker / "x.ctl")])
    (t,) = load_config(str(isolated_user_config))
    assert t.control_file == str(tmp_path / "d1.ctl")  # previous control file restored


def test_log_command(user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    monkeypatch.chdir(tmp_path)
    cli.main(["log"])
    assert "(no entries yet)" in capsys.readouterr().out
    for _ in range(3):
        cli.main(["d1"])
    capsys.readouterr()
    cli.main(["log", "-n", "2"])
    out = capsys.readouterr().out
    assert out.startswith(f"Log file: {user_log}")
    shown = [l for l in out.splitlines() if fmon_log.is_entry_start(l)]
    assert len(shown) == 2
    cli.main(["log", "-n", "0"])
    assert len([l for l in capsys.readouterr().out.splitlines() if fmon_log.is_entry_start(l)]) == 5


def test_log_command_shows_full_report_blocks(user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    monkeypatch.chdir(tmp_path)
    cli.main(["d1"])
    (tmp_path / "d1" / "new.txt").write_text("x")
    (tmp_path / "d1" / "gone.txt").write_text("y")
    with pytest.raises(SystemExit):
        cli.main(["-c", "d1"])
    cli.main(["d1"])  # new baseline
    (tmp_path / "d1" / "gone.txt").unlink()
    (tmp_path / "d1" / "new.txt").write_text("longer content")
    with pytest.raises(SystemExit):
        cli.main(["-c", "d1"])
    capsys.readouterr()
    cli.main(["log", "-n", "1"])
    out = capsys.readouterr().out
    assert "Removed: 1" in out and "gone.txt" in out
    assert "Modified: 1" in out and "new.txt" in out and "size:  1 B -> 14 B  *" in out


def test_log_location_and_rotation_from_config(isolated_user_config, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    monkeypatch.chdir(tmp_path)
    logdir = tmp_path / "audit"
    isolated_user_config.parent.mkdir(parents=True)
    isolated_user_config.write_text(
        f"[fmon]\nlog_file = {logdir / 'fmon.log'}\nlog_max_size = 400\nlog_keep_days = 30\n"
    )
    for _ in range(6):
        cli.main(["d1"])
    names = sorted(p.name for p in logdir.iterdir())
    assert len(names) >= 2 and names[0] == "fmon.log"
    assert all(re.match(r"fmon_\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d_to_\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d(_\d+)?\.log$", n)
               for n in names[1:])
    assert os.path.getsize(logdir / "fmon.log") <= 400
    assert len(entries(logdir / "fmon.log")) < 6
    cli.main(["log", "--files"])
    out = capsys.readouterr().out
    assert f"Rotated logs: {len(names) - 1}" in out and names[1] in out
    cli.main(["config"])
    assert f"Log file: {logdir / 'fmon.log'}" in capsys.readouterr().out


def test_logging_can_be_disabled(isolated_user_config, user_log, tmp_path, monkeypatch, capsys):
    (tmp_path / "d1").mkdir()
    monkeypatch.chdir(tmp_path)
    isolated_user_config.parent.mkdir(parents=True)
    isolated_user_config.write_text("[fmon]\nlog_file = off\n")
    cli.main(["d1"])
    assert not user_log.exists()
    with pytest.raises(SystemExit) as exc:
        cli.main(["log"])
    assert exc.value.code == 2
    assert "logging is disabled" in capsys.readouterr().err


def test_help_is_not_logged(user_log, capsys):
    cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["-h"])
    assert not user_log.exists()
