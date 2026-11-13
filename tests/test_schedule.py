import os
import sys
import time
import pytest
from fmon import config as fmon_config
from fmon import schedule as fmon_schedule
from fmon.config import parse_interval, format_interval, load_config, ConfigError
from fmon import cli


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    path = tmp_path / "userhome" / "fmon" / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(path))
    monkeypatch.chdir(tmp_path)
    return path


@pytest.mark.parametrize("value, seconds", [
    ("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400), ("45", 45), (" 5 m ", 300),
    ("off", None), ("", None), ("0", None), ("never", None),
])
def test_parse_interval(value, seconds):
    assert parse_interval(value) == seconds


@pytest.mark.parametrize("value", ["1x", "abc", "-5m", "1.5h", "3s"])
def test_parse_interval_invalid(value):
    with pytest.raises(ValueError):
        parse_interval(value)


@pytest.mark.parametrize("seconds, text", [(None, "off"), (30, "30s"), (900, "15m"), (7200, "2h"), (86400, "1d"), (90, "90s")])
def test_format_interval(seconds, text):
    assert format_interval(seconds) == text


def test_schedule_option_in_config(isolated_user_config, tmp_path, capsys):
    (tmp_path / "web").mkdir()
    cli.main(["web", "-n", "website"])
    cli.main(["config", "set", "website", "schedule", "15m"])
    (t,) = load_config(str(isolated_user_config))
    assert t.schedule == 900
    cli.main(["config", "show", "website"])
    assert "schedule:     every 15m" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["config", "set", "website", "schedule", "soon"])
    assert "invalid interval 'soon'" in capsys.readouterr().err
    assert load_config(str(isolated_user_config))[0].schedule == 900  # unchanged

    cli.main(["config", "set", "website", "schedule", "off"])
    assert load_config(str(isolated_user_config))[0].schedule is None


def test_fmon_executable_frozen(monkeypatch, tmp_path):
    assert fmon_schedule.fmon_executable() == [sys.executable, "-m", "fmon"]
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / ("fmon-gui.exe" if os.name == "nt" else "fmon-gui")))
    assert fmon_schedule.fmon_executable() == [sys.executable]          # no CLI next to it: use itself
    cli_exe = exe_dir / ("fmon.exe" if os.name == "nt" else "fmon")
    cli_exe.write_text("")
    assert fmon_schedule.fmon_executable() == [str(cli_exe)]             # the console CLI next to the GUI
    assert fmon_schedule.fmon_command(["check"], "/c.ini") == [str(cli_exe), "check", "-f", "/c.ini"]


def test_fmon_command_adds_config():
    cmd = fmon_schedule.fmon_command(["check", "-t", "x"], "/c.ini")
    assert cmd[1:] == ["-m", "fmon", "check", "-t", "x", "-f", "/c.ini"]
    cmd = fmon_schedule.fmon_command(["check", "-f", "other.ini"], "/c.ini")
    assert cmd[-2:] == ["-f", "other.ini"]


def test_run_fmon_subprocess_is_logged_with_origin(isolated_user_config, tmp_path):
    (tmp_path / "d").mkdir()
    proc = fmon_schedule.run_fmon(["d"], str(isolated_user_config), origin="gui")
    assert proc.returncode == 0, proc.stderr
    assert "Snapshot of" in proc.stdout
    log = (isolated_user_config.parent / "fmon.log").read_text()
    assert "| [gui] " in log and "fmon d" in log


def _write_config(path, targets):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "[fmon]\nlog_file = off\n"
    for name, folder, schedule in targets:
        text += f"\n[{name}]\npath = {folder}\ncontrol_file = {folder}.ctl\n"
        if schedule:
            text += f"schedule = {schedule}\n"
    path.write_text(text)


def test_scheduler_runs_jobs_with_fake_runner(isolated_user_config, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _write_config(isolated_user_config, [("a", tmp_path / "a", "5s"), ("b", tmp_path / "b", None)])
    runs = []
    events = []

    def runner(name):
        runs.append(name)
        return (1, "Added: 1\nRemoved: 0\nModified: 0\n")

    s = fmon_schedule.Scheduler(str(isolated_user_config), on_event=events.append, runner=runner, reload_every=1)
    s.reload()
    assert list(s.jobs) == ["a"]  # b has no schedule
    assert s.jobs["a"].interval == 5
    s.run_now("a")
    s.start()
    try:
        deadline = time.time() + 5
        while not runs and time.time() < deadline:
            time.sleep(0.05)
        assert runs == ["a"]
        deadline = time.time() + 5
        while not any(e.kind == "finished" for e in events) and time.time() < deadline:
            time.sleep(0.05)
        state = s.snapshot()["a"]
        assert state.last_status == 1
        assert state.last_summary == "1 added, 0 removed, 0 modified"
        assert state.status_text == "CHANGES"
        assert state.next_run > time.time() + 3
        kinds = [e.kind for e in events]
        assert "reloaded" in kinds and "started" in kinds and "finished" in kinds
    finally:
        s.stop()
    assert not s.running


def test_scheduler_reload_picks_up_changes(isolated_user_config, tmp_path):
    (tmp_path / "a").mkdir()
    _write_config(isolated_user_config, [("a", tmp_path / "a", "1h")])
    s = fmon_schedule.Scheduler(str(isolated_user_config), runner=lambda n: (0, ""))
    s.reload()
    assert s.jobs["a"].interval == 3600
    _write_config(isolated_user_config, [("a", tmp_path / "a", "5m")])
    s.reload()
    assert s.jobs["a"].interval == 300
    _write_config(isolated_user_config, [("a", tmp_path / "a", None)])
    s.reload()
    assert s.jobs == {}


def test_scheduler_survives_runner_exception(isolated_user_config, tmp_path):
    (tmp_path / "a").mkdir()
    _write_config(isolated_user_config, [("a", tmp_path / "a", "5s")])

    def runner(name):
        raise RuntimeError("boom")

    s = fmon_schedule.Scheduler(str(isolated_user_config), runner=runner)
    s.reload()
    s.run_now("a")
    s.start()
    try:
        deadline = time.time() + 5
        while s.snapshot()["a"].last_status is None and time.time() < deadline:
            time.sleep(0.05)
        state = s.snapshot()["a"]
        assert state.last_status == 2 and "boom" in state.last_output
        assert s.running
    finally:
        s.stop()


def test_scheduler_reports_config_error(isolated_user_config):
    isolated_user_config.parent.mkdir(parents=True)
    isolated_user_config.write_text("[a]\npath = x\nschedule = nonsense\n")
    events = []
    s = fmon_schedule.Scheduler(str(isolated_user_config), on_event=events.append)
    s.reload()
    assert events[-1].kind == "error" and "schedule" in events[-1].message


def test_summarize():
    assert fmon_schedule._summarize("x\nAdded: 2\nRemoved: 1\nModified: 0\n", 1) == "2 added, 1 removed, 0 modified"
    assert fmon_schedule._summarize("error: Control file not found: x\n", 2) == "Control file not found: x"
    assert fmon_schedule._summarize("", 0) == "no changes"


def test_watch_command(isolated_user_config, tmp_path, capsys):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "f.txt").write_text("x")
    cli.main(["web"])
    with pytest.raises(SystemExit) as exc:  # nothing scheduled
        cli.main(["watch"])
    assert exc.value.code == 2
    assert "no target has a schedule" in capsys.readouterr().err

    cli.main(["config", "set", "web", "schedule", "5s"])
    capsys.readouterr()
    cli.main(["watch", "-i", "--max-runtime", "3"])
    out = capsys.readouterr().out
    assert "[web] every 5s" in out
    assert "[web] checking..." in out and "[web] no changes" in out
    log = (isolated_user_config.parent / "fmon.log").read_text()
    assert "[scheduled] fmon check -t web" in log
    assert "fmon watch -i --max-runtime 3 | watch stopped" in log
