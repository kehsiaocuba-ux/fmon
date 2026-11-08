import os
import pytest
from fmon import config as fmon_config
from fmon.config import ConfigError, load_config, user_config_path
from fmon.scanner import FolderScanner
from fmon import cli


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Point the per-user config at a temp location so tests never touch
    (or depend on) the developer's real ~/.config/fmon/fmon.ini."""
    path = tmp_path / "userhome" / "fmon" / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(path))
    return path


def write_ini(tmp_path, text, name="fmon.ini"):
    ini = tmp_path / name
    ini.write_text(text)
    return ini


def test_load_config_defaults(tmp_path):
    (tmp_path / "usr").mkdir()
    (tmp_path / "home").mkdir()
    ini = write_ini(tmp_path, f"""
[usr]
path = {tmp_path / "usr"}

[home]
path = {tmp_path / "home"}
""")
    targets = load_config(str(ini))
    assert [t.name for t in targets] == ["usr", "home"]
    assert targets[0].path == str(tmp_path / "usr")
    # default control file: <ini dir>/<name>.ctl
    assert targets[0].control_file == str(tmp_path / "usr.ctl")
    assert targets[1].control_file == str(tmp_path / "home.ctl")


def test_load_config_control_dir_and_relative_paths(tmp_path):
    ini = write_ini(tmp_path, """
[fmon]
control_dir = controls

[data]
path = data

[logs]
path = ./logs
control_file = logs-baseline.ctl

[abs]
path = abs
control_file = %s
""" % (tmp_path / "elsewhere" / "abs.ctl"))
    targets = {t.name: t for t in load_config(str(ini))}
    assert targets["data"].path == str(tmp_path / "data")
    assert targets["data"].control_file == str(tmp_path / "controls" / "data.ctl")
    assert targets["logs"].control_file == str(tmp_path / "controls" / "logs-baseline.ctl")
    assert targets["abs"].control_file == str(tmp_path / "elsewhere" / "abs.ctl")


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.ini"))


def test_duplicate_target_name(tmp_path):
    ini = write_ini(tmp_path, "[a]\npath = x\n\n[a]\npath = y\n")
    with pytest.raises(ConfigError, match=r"duplicate target name \[a\]"):
        load_config(str(ini))


def test_conflicting_control_files(tmp_path):
    ini = write_ini(tmp_path, """
[first]
path = x
control_file = shared.ctl

[second]
path = y
control_file = ./shared.ctl
""")
    with pytest.raises(ConfigError, match=r"\[first\] and \[second\] use the same control file"):
        load_config(str(ini))


def test_conflicting_control_files_via_default_name(tmp_path):
    ini = write_ini(tmp_path, """
[usr]
path = x

[other]
path = y
control_file = usr.ctl
""")
    with pytest.raises(ConfigError, match="same control file"):
        load_config(str(ini))


@pytest.mark.parametrize("name", ["../evil", "with space", "a/b", ".hidden", ""])
def test_invalid_target_name(tmp_path, name):
    ini = write_ini(tmp_path, f"[{name}]\npath = x\n")
    with pytest.raises(ConfigError):
        load_config(str(ini))


def test_missing_path_option(tmp_path):
    ini = write_ini(tmp_path, "[usr]\ncontrol_file = usr.ctl\n")
    with pytest.raises(ConfigError, match="missing required option 'path'"):
        load_config(str(ini))


def test_unknown_option(tmp_path):
    ini = write_ini(tmp_path, "[usr]\npath = x\nfoo = bar\n")
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(str(ini))


def test_no_targets(tmp_path):
    ini = write_ini(tmp_path, "[fmon]\ncontrol_dir = c\n")
    with pytest.raises(ConfigError, match="no targets"):
        load_config(str(ini))


def test_control_file_inside_monitored_folder_is_excluded(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "f.txt").write_text("x")
    ini = write_ini(tmp_path, f"""
[data]
path = data
control_file = {tmp_path / "data" / "data.ctl"}
""")
    (target,) = load_config(str(ini))
    scanner = FolderScanner.for_target(target)
    scanner.save_control_file(target.control_file)
    diff = scanner.check(target.control_file)
    assert not any(diff.values())  # the .ctl itself is not reported as added


# --- CLI -----------------------------------------------------------------

def _setup(tmp_path):
    for name in ("usr", "home"):
        d = tmp_path / name
        d.mkdir()
        (d / "file.txt").write_text(name)
    (tmp_path / "controls").mkdir()
    return write_ini(tmp_path, f"""
[fmon]
control_dir = controls

[usr]
path = {tmp_path / "usr"}

[home]
path = {tmp_path / "home"}
""")


def test_cli_config_snapshot_and_check(tmp_path, capsys):
    ini = _setup(tmp_path)

    cli.main(["snapshot", "-f", str(ini)])
    assert (tmp_path / "controls" / "usr.ctl").exists()
    assert (tmp_path / "controls" / "home.ctl").exists()
    out = capsys.readouterr().out
    assert "[usr] Snapshot of" in out and "[home] Snapshot of" in out

    cli.main(["check", "-f", str(ini)])  # exit 0
    out = capsys.readouterr().out
    assert f"fmon check [usr]: {tmp_path / 'usr'}" in out
    assert f"fmon check [home]: {tmp_path / 'home'}" in out
    assert out.count("No changes detected.") == 2

    (tmp_path / "home" / "new.txt").write_text("!")
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "-f", str(ini)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "new.txt" in out
    assert out.count("No changes detected.") == 1  # usr still clean


def test_cli_config_select_target(tmp_path, capsys):
    ini = _setup(tmp_path)
    cli.main(["snapshot", "-f", str(ini), "-t", "home"])
    assert (tmp_path / "controls" / "home.ctl").exists()
    assert not (tmp_path / "controls" / "usr.ctl").exists()

    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "-f", str(ini), "-t", "nope"])
    assert exc.value.code == 2
    assert "unknown target(s): nope" in capsys.readouterr().err


def test_cli_config_check_continues_after_error(tmp_path, capsys):
    ini = _setup(tmp_path)
    cli.main(["snapshot", "-f", str(ini), "-t", "home"])  # no usr.ctl
    (tmp_path / "home" / "new.txt").write_text("!")

    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "-f", str(ini)])
    assert exc.value.code == 2  # error outranks "changes"
    captured = capsys.readouterr()
    assert "[usr] Control file not found" in captured.err
    assert "new.txt" in captured.out  # home was still checked


def test_cli_config_error_exit_code(tmp_path, capsys):
    ini = write_ini(tmp_path, "[a]\npath = x\n\n[b]\npath = y\ncontrol_file = a.ctl\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "-f", str(ini)])
    assert exc.value.code == 2
    assert "same control file" in capsys.readouterr().err


def test_cli_snapshot_missing_dir(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshot", str(tmp_path / "nope"), "-o", str(tmp_path / "x.ctl")])
    assert exc.value.code == 2
    assert "Directory not found" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["snapshot"],                                   # nothing given, no user config
    ["check", "dir", "-f", "x.ini"],                # both
    ["snapshot", "dir", "-t", "usr"],               # --target with a directory
    ["check", "dir"],                               # --control missing
    ["snapshot", "-f", "x.ini", "-o", "out.ctl"],   # --output with --config
    ["check", "-f", "x.ini", "-c", "out.ctl"],      # --control with --config
])
def test_cli_usage_errors(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the log for '-f x.ini' would land next to it
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


# --- bare invocation, shortcuts, per-user config ---------------------------

def test_bare_fmon_prints_manual(capsys):
    cli.main([])  # no SystemExit, exit code 0
    out = capsys.readouterr().out
    assert out.startswith("FMON(1)")
    for section in ("SYNOPSIS", "COMMANDS", "SNAPSHOT", "CHECK", "CONFIG", "LOG", "Examples:",
                    "CONFIGURATION FILE", "SECURITY", "EXIT STATUS"):
        assert section in out, section


@pytest.mark.parametrize("argv, expected", [
    (["test1", "-o", "test1.ctl"], ["snapshot", "test1", "-o", "test1.ctl"]),
    (["test1"], ["snapshot", "test1"]),
    (["test1", "-c", "x.ctl"], ["check", "test1", "-c", "x.ctl"]),
    (["-c", "test1"], ["check", "-c", "test1"]),
    (["test1", "-c"], ["check", "test1", "-c"]),
    (["test1", "--control=x.ctl"], ["check", "test1", "--control=x.ctl"]),
    (["snapshot", "d"], ["snapshot", "d"]),
    (["check", "-f", "a.ini"], ["check", "-f", "a.ini"]),
    (["-h"], ["-h"]),
    ([], []),
])
def test_normalize_argv(argv, expected):
    assert cli.normalize_argv(argv) == expected


def test_shortcut_snapshot_in_cwd(tmp_path, monkeypatch, capsys):
    (tmp_path / "test1").mkdir()
    (tmp_path / "test1" / "f.txt").write_text("x")
    monkeypatch.chdir(tmp_path)

    cli.main(["test1", "-o", "test1.ctl"])
    assert (tmp_path / "test1.ctl").exists()
    assert f"saved to {tmp_path / 'test1.ctl'}" in capsys.readouterr().out

    cli.main(["test1", "-c", "test1.ctl"])
    assert "No changes detected." in capsys.readouterr().out


def _check_ok(argv, capsys):
    cli.main(argv)
    out = capsys.readouterr().out
    assert "No changes detected." in out
    return out


def test_check_flexible_control_argument(tmp_path, monkeypatch, capsys):
    """-c accepts a control file or a directory, in any order, or no value."""
    (tmp_path / "test1").mkdir()
    (tmp_path / "test1" / "f.txt").write_text("x")
    monkeypatch.chdir(tmp_path)

    cli.main(["test1"])  # default output: <DIR>.ctl next to the folder
    assert (tmp_path / "test1.ctl").exists()
    capsys.readouterr()

    expected_ctl = f"Control file: {tmp_path / 'test1.ctl'}"
    expected_dir = f"fmon check: {tmp_path / 'test1'}"
    for argv in (
        ["test1", "-c", "test1.ctl"],
        ["-c", "test1.ctl"],
        ["-c", "test1"],
        ["test1", "-c"],
        ["-c", "test1.ctl", "test1"],
        ["-c", "test1", "test1.ctl"],
        ["check", "-c", "test1"],
        ["check", "test1", "--control=test1.ctl"],
    ):
        out = _check_ok(argv, capsys)
        assert expected_ctl in out and expected_dir in out, argv


def test_check_with_explicit_control_elsewhere(tmp_path, monkeypatch, capsys):
    (tmp_path / "test1").mkdir()
    (tmp_path / "test1" / "f.txt").write_text("x")
    (tmp_path / "ctl").mkdir()
    monkeypatch.chdir(tmp_path)
    cli.main(["test1", "-o", "ctl/other.ctl"])
    capsys.readouterr()
    out = _check_ok(["-c", "ctl/other.ctl", "test1"], capsys)
    assert f"Control file: {tmp_path / 'ctl' / 'other.ctl'}" in out


@pytest.mark.parametrize("argv, message", [
    (["-c", "nope.ctl"], "neither a directory nor an existing control file"),
    (["-c", "orphan.ctl"], "cannot infer the folder"),     # exists, but no 'orphan' dir
    (["-c", "test1", "test1"], "two directories"),
    (["nope", "-c", "nope2.ctl"], "is a directory"),
])
def test_check_control_argument_errors(tmp_path, monkeypatch, capsys, argv, message):
    (tmp_path / "test1").mkdir()
    (tmp_path / "orphan.ctl").write_text("{}")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_check_missing_default_control(tmp_path, monkeypatch, capsys):
    (tmp_path / "test1").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main(["-c", "test1"])
    assert exc.value.code == 2
    assert f"Control file not found: {tmp_path / 'test1.ctl'}" in capsys.readouterr().err


def test_user_config_path_platform(monkeypatch):
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        assert user_config_path() == r"C:\Users\x\AppData\Roaming\fmon\fmon.ini"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
        assert user_config_path() == "/xdg/fmon/fmon.ini"
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.setenv("HOME", "/home/u")
        assert user_config_path() == "/home/u/.config/fmon/fmon.ini"


def test_no_args_without_user_config_is_error(isolated_user_config, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["check"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(isolated_user_config) in err and "fmon config init" in err


def test_config_command(isolated_user_config, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["config"])
    assert exc.value.code == 2
    assert "not found" in capsys.readouterr().out

    cli.main(["config", "init"])
    assert isolated_user_config.exists()
    assert "Created" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:  # refuse to overwrite
        cli.main(["config", "init"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:  # --init alias still works
        cli.main(["config", "--init"])
    assert exc.value.code == 2

    (tmp_path / "data").mkdir()
    with open(isolated_user_config, "a") as f:
        f.write(f"\n[data]\npath = {tmp_path / 'data'}\n")
    cli.main(["config"])
    out = capsys.readouterr().out
    assert "[data]" in out
    assert str(isolated_user_config.parent / "controls" / "data.ctl") in out


def test_user_config_used_by_default(isolated_user_config, tmp_path, capsys):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "f.txt").write_text("x")
    isolated_user_config.parent.mkdir(parents=True)
    isolated_user_config.write_text(
        f"[fmon]\ncontrol_dir = controls\n\n[data]\npath = {tmp_path / 'data'}\n"
    )

    cli.main(["snapshot"])  # control_dir is created on demand
    ctl = isolated_user_config.parent / "controls" / "data.ctl"
    assert ctl.exists()

    cli.main(["check", "-t", "data"])
    assert "No changes detected." in capsys.readouterr().out

    (tmp_path / "data" / "g.txt").write_text("y")
    with pytest.raises(SystemExit) as exc:
        cli.main(["check"])
    assert exc.value.code == 1


def test_same_folder_monitored_by_two_configs(tmp_path, capsys):
    """Two users may watch the same folder, each with their own control file."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "f.txt").write_text("x")
    inis = []
    for user in ("alice", "bob"):
        home = tmp_path / user
        home.mkdir()
        ini = home / "fmon.ini"
        ini.write_text(f"[shared]\npath = {tmp_path / 'shared'}\n")
        inis.append(ini)
        cli.main(["snapshot", "-f", str(ini)])
        assert (home / "shared.ctl").exists()

    (tmp_path / "shared" / "f.txt").write_text("changed!")
    for ini in inis:
        with pytest.raises(SystemExit) as exc:
            cli.main(["check", "-f", str(ini)])
        assert exc.value.code == 1
        assert "f.txt" in capsys.readouterr().out


# --- config editing ---------------------------------------------------------

@pytest.fixture
def user_ini(isolated_user_config, tmp_path):
    cli.main(["config", "init"])
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    return isolated_user_config


def test_config_show_displays_contents_and_targets(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    capsys.readouterr()
    cli.main(["config"])
    out = capsys.readouterr().out
    assert f"Config file: {user_ini}" in out
    assert "Writable by you: yes" in out
    assert "[d1]" in out and f"path = {tmp_path / 'd1'}" in out  # raw contents
    assert f"control file: {user_ini.parent / 'controls' / 'd1.ctl'}" in out  # resolved
    cli.main(["config", "show"])  # explicit action is the same
    assert "Writable by you" in capsys.readouterr().out


def test_config_add_remove(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    cli.main(["config", "add", "d2", str(tmp_path / "d2"), "-c", "two.ctl"])
    out = capsys.readouterr().out
    assert "Added target [d1]" in out and "two.ctl" in out
    names = [t.name for t in load_config(str(user_ini))]
    assert names == ["d1", "d2"]

    with pytest.raises(SystemExit):  # duplicate name
        cli.main(["config", "add", "d1", str(tmp_path / "d2")])
    assert "already exists" in capsys.readouterr().err

    with pytest.raises(SystemExit):  # control-file conflict
        cli.main(["config", "add", "d3", str(tmp_path / "d2"), "-c", "d1.ctl"])
    assert "same control file" in capsys.readouterr().err
    assert [t.name for t in load_config(str(user_ini))] == ["d1", "d2"]  # nothing written

    cli.main(["config", "remove", "d1"])
    assert "Deleted target [d1]" in capsys.readouterr().out
    assert [t.name for t in load_config(str(user_ini))] == ["d2"]

    with pytest.raises(SystemExit):
        cli.main(["config", "remove", "nope"])
    assert "no target named 'nope'" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # [fmon] is not a target
        cli.main(["config", "remove", "fmon"])


def test_config_set_unset(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    cli.main(["config", "set", "fmon", "control_dir", str(tmp_path / "ctl")])
    cli.main(["config", "set", "d1", "control_file", "custom.ctl"])
    cli.main(["config", "set", "d1", "path", str(tmp_path / "d2")])
    capsys.readouterr()
    (t,) = load_config(str(user_ini))
    assert t.path == str(tmp_path / "d2")
    assert t.control_file == str(tmp_path / "ctl" / "custom.ctl")

    cli.main(["config", "unset", "d1", "control_file"])
    cli.main(["config", "unset", "fmon", "control_dir"])
    (t,) = load_config(str(user_ini))
    assert t.control_file == str(user_ini.parent / "d1.ctl")

    for argv, msg in [
        (["config", "set", "d1", "bogus", "1"], "unknown option 'bogus'"),
        (["config", "set", "fmon", "path", "x"], "unknown option 'path'"),
        (["config", "unset", "d1", "path"], "missing required option 'path'"),
        (["config", "unset", "d1", "nothing"], "no option 'nothing'"),
        (["config", "unset", "zzz", "path"], "no section [zzz]"),
        (["config", "set", "bad name", "path", "x"], "invalid target name"),
    ]:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2, argv
        assert msg in capsys.readouterr().err, argv
    (t,) = load_config(str(user_ini))  # still valid, nothing half-written
    assert t.path == str(tmp_path / "d2")


def test_config_set_global_option_by_name(user_ini, capsys):
    cli.main(["config", "set", "log_max_size", "5M"])
    cli.main(["config", "update", "log_keep_days", "10"])    # 'update' == 'set'
    cli.main(["config", "set", "LOG_FILE", "audit.log"])     # case-insensitive option
    capsys.readouterr()
    settings = fmon_config.log_settings(str(user_ini))
    assert settings.max_size == 5 * 1024 ** 2 and settings.keep_days == 10
    assert settings.path == str(user_ini.parent / "audit.log")
    text = user_ini.read_text()
    assert "log_max_size = 5M" in text and "; fmon per-user configuration" in text

    cli.main(["config", "unset", "log_max_size"])
    assert fmon_config.log_settings(str(user_ini)).max_size == fmon_config.DEFAULT_LOG_MAX_SIZE

    for argv, msg in [
        (["config", "set", "bogus", "1"], "'bogus' is not a [fmon] setting"),
        (["config", "set", "log_max_size", "lots"], "invalid size 'lots'"),
        (["config", "set", "log_keep_days", "-3"], "log_keep_days must be"),
        (["config", "set", "log_max_size"], "usage: fmon config set"),
        (["config", "set", "a", "b", "c", "d"], "usage: fmon config set"),
        (["config", "unset", "nothing"], "'nothing' is not a [fmon] setting"),
        (["config", "unset", "log_max_size"], "no option 'log_max_size'"),  # already unset above
    ]:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2, argv
        assert msg in capsys.readouterr().err, argv


def test_config_get(user_ini, tmp_path, capsys):
    cli.main(["config", "get"])
    out = capsys.readouterr().out
    assert f"Config file: {user_ini}" in out
    assert "control_dir   = controls  -> " + str(user_ini.parent / "controls") in out
    assert "log_file      = " + str(user_ini.parent / "fmon.log") + "  (default)" in out
    assert "log_max_size  = 1048576 bytes  (default)" in out
    assert "log_keep_days = 0 (keep rotated logs forever)  (default)" in out

    cli.main(["config", "set", "log_max_size", "2K"])
    cli.main(["config", "get", "log_max_size", "log_keep_days"])
    out = capsys.readouterr().out
    assert "log_max_size  = 2K  -> 2048 bytes" in out and "log_keep_days" in out
    assert "control_dir" not in out

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "get", "nope"])
    assert exc.value.code == 2
    assert "unknown setting(s): nope" in capsys.readouterr().err


def test_config_get_without_config_file(isolated_user_config, capsys):
    cli.main(["config", "get"])
    out = capsys.readouterr().out
    assert "(not found - showing defaults)" in out
    assert "log_keep_days = 0 (keep rotated logs forever)  (default)" in out


def test_config_set_creates_target_section(user_ini, tmp_path, capsys):
    cli.main(["config", "set", "new", "path", str(tmp_path / "d1")])
    (t,) = load_config(str(user_ini))
    assert t.name == "new"


def test_config_edit_explicit_file(tmp_path, capsys):
    ini = tmp_path / "site.ini"
    cli.main(["config", "init", "-f", str(ini)])
    cli.main(["config", "add", "d", str(tmp_path), "-f", str(ini)])
    cli.main(["config", "-f", str(ini)])
    assert f"Config file: {ini}" in capsys.readouterr().out
    assert [t.name for t in load_config(str(ini))] == ["d"]


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_config_readonly_is_not_permitted(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    capsys.readouterr()
    os.chmod(user_ini, 0o444)
    try:
        before = user_ini.read_text()

        cli.main(["config"])  # reading is still fine
        assert "Writable by you: no (read-only)" in capsys.readouterr().out

        for argv in (
            ["config", "add", "d2", str(tmp_path / "d2")],
            ["config", "remove", "d1"],
            ["config", "set", "fmon", "control_dir", "x"],
            ["config", "unset", "d1", "path"],
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main(argv)
            assert exc.value.code == 2, argv
            err = capsys.readouterr().err
            assert "not permitted to modify" in err and "administrator" in err, argv
        assert user_ini.read_text() == before

        # but the user can still run checks with the admin-controlled config
        cli.main(["snapshot"])
        cli.main(["check"])
        assert "No changes detected." in capsys.readouterr().out
    finally:
        os.chmod(user_ini, 0o644)


# --- IniText (comment-preserving editor) ------------------------------------

SAMPLE = """; header comment

[fmon]
; about control_dir
control_dir = controls

[a]
path = /a

[b]
path = /b
control_file = b.ctl
"""


def test_initext_set_and_preserve_comments():
    ini = fmon_config.IniText(SAMPLE)
    ini.set("fmon", "control_dir", "/new")          # replace existing
    ini.set("a", "control_file", "a.ctl")           # append to section
    ini.set("c", "path", "/c")                      # new section at end
    text = ini.text
    assert "; header comment" in text and "; about control_dir" in text
    assert "control_dir = /new" in text and "control_dir = controls" not in text
    assert "[a]\npath = /a\ncontrol_file = a.ctl\n\n[b]" in text
    assert text.endswith("control_file = b.ctl\n\n[c]\npath = /c\n")
    parser = fmon_config._parse_text(text, "x.ini")
    assert parser["a"]["control_file"] == "a.ctl" and parser["c"]["path"] == "/c"


def test_initext_remove():
    ini = fmon_config.IniText(SAMPLE)
    assert ini.remove_option("b", "control_file")
    assert not ini.remove_option("b", "nothing")
    assert ini.remove_section("a")
    assert not ini.remove_section("zzz")
    text = ini.text
    assert "[a]" not in text and "/a" not in text
    assert "control_file" not in text
    assert "\n\n\n" not in text  # no double blank lines left behind
    assert ini.section_text("b") == "[b]\npath = /b\n"
    assert ini.section_text("a") is None


def test_initext_case_insensitive_options_and_commented_sections():
    ini = fmon_config.IniText("[a]\nPath = /a\n; [b]\n; path = /b\n")
    assert ini.has_option("a", "path")
    assert not ini.has_section("b")
    assert fmon_config.IniText("[c] ; trailing note\npath = /c\n").has_section("c")
    ini.set("a", "path", "/x")
    assert ini.text == "[a]\npath = /x\n; [b]\n; path = /b\n"


def test_config_edits_keep_comments(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    cli.main(["config", "set", "fmon", "control_dir", "elsewhere"])
    cli.main(["config", "delete", "d1"])
    text = user_ini.read_text()
    assert "; fmon per-user configuration" in text
    assert "control_dir = elsewhere" in text
    assert "[d1]" not in text


# --- automatic registration of control files --------------------------------

def test_snapshot_creates_and_populates_user_config(isolated_user_config, tmp_path, monkeypatch, capsys):
    (tmp_path / "test3").mkdir()
    (tmp_path / "test3" / "f.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    assert not isolated_user_config.exists()

    cli.main(["test3", "-o", "test3.ctl"])
    out = capsys.readouterr().out
    assert "[test3] Snapshot of" in out
    assert f"[test3] Target added in {isolated_user_config}" in out
    assert isolated_user_config.exists()
    text = isolated_user_config.read_text()
    assert "; fmon per-user configuration" in text  # template comments kept
    (t,) = load_config(str(isolated_user_config))
    assert t.name == "test3"
    assert t.path == str(tmp_path / "test3")
    assert t.control_file == str(tmp_path / "test3.ctl")

    cli.main(["config", "show", "test3"])
    out = capsys.readouterr().out
    assert "[test3]" in out and "exists" in out

    # the registered target can now be checked via the config
    cli.main(["check", "-t", "test3"])
    assert "No changes detected." in capsys.readouterr().out


def test_snapshot_registration_rules(isolated_user_config, tmp_path, monkeypatch, capsys):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f.txt").write_text(d)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a").mkdir()
    monkeypatch.chdir(tmp_path)

    cli.main(["a"])
    cli.main(["a"])  # same folder, same control file -> unchanged, no duplicate
    assert "Target" not in capsys.readouterr().out.splitlines()[-1]
    assert [t.name for t in load_config(str(isolated_user_config))] == ["a"]

    cli.main(["a", "-o", "a2.ctl"])  # same folder, new control file -> updated
    assert "[a] Target updated" in capsys.readouterr().out
    (t,) = load_config(str(isolated_user_config))
    assert t.control_file == str(tmp_path / "a2.ctl")

    cli.main(["sub/a"])  # different folder, same base name -> suffixed name
    assert "[a-2] Target added" in capsys.readouterr().out

    cli.main(["b", "-n", "bee"])  # explicit name
    assert "[bee] Target added" in capsys.readouterr().out
    assert [t.name for t in load_config(str(isolated_user_config))] == ["a", "a-2", "bee"]

    with pytest.raises(SystemExit) as exc:  # control file already used for another folder
        cli.main(["b", "-o", "a2.ctl"])
    assert exc.value.code == 2
    assert "already used by target [a]" in capsys.readouterr().err

    with pytest.raises(SystemExit):  # explicit name taken by another folder
        cli.main(["b", "-n", "a"])
    assert "already watches a different folder" in capsys.readouterr().err

    with pytest.raises(SystemExit):  # missing dir: nothing registered
        cli.main(["nope"])
    assert "Directory not found" in capsys.readouterr().err
    assert [t.name for t in load_config(str(isolated_user_config))] == ["a", "a-2", "bee"]


def test_snapshot_registers_in_explicit_config(tmp_path, monkeypatch, capsys):
    (tmp_path / "d").mkdir()
    monkeypatch.chdir(tmp_path)
    ini = tmp_path / "site.ini"
    cli.main(["d", "-f", str(ini)])
    assert f"Target added in {ini}" in capsys.readouterr().out
    assert [t.name for t in load_config(str(ini))] == ["d"]


@pytest.mark.parametrize("path, expected", [
    ("/srv/www", "www"), ("/srv/www/", "www"), ("/x/my folder!", "my-folder-"),
    ("/x/..hidden", "hidden"), ("/", "target"),
])
def test_suggest_name(path, expected):
    assert fmon_config.suggest_name(path) == expected


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_snapshot_refused_when_config_readonly(user_ini, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli.main(["d1"])  # registered while writable
    capsys.readouterr()
    os.chmod(user_ini, 0o444)
    try:
        cli.main(["d1"])  # re-snapshot of a registered target needs no config change
        assert "Snapshot of" in capsys.readouterr().out

        with pytest.raises(SystemExit) as exc:  # new folder -> would need to edit config
            cli.main(["d2"])
        assert exc.value.code == 2
        assert "not permitted to modify" in capsys.readouterr().err
        assert not (tmp_path / "d2.ctl").exists()  # no unregistered control file created
    finally:
        os.chmod(user_ini, 0o644)


# --- config show PATTERN / delete -------------------------------------------

@pytest.fixture
def populated(user_ini, tmp_path, monkeypatch, capsys):
    for d in ("test1", "test2", "other"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f.txt").write_text(d)
    monkeypatch.chdir(tmp_path)
    for d in ("test1", "test2", "other"):
        cli.main([d])
    capsys.readouterr()
    return user_ini


def test_config_show_single_target(populated, tmp_path, capsys):
    cli.main(["config", "show", "test1"])
    out = capsys.readouterr().out
    assert "Targets matching test1: 1" in out
    assert "[test1]" in out and "[test2]" not in out and "[other]" not in out
    assert f"path = {tmp_path / 'test1'}" in out                       # raw section
    assert f"-> control file: {tmp_path / 'test1.ctl'}  (exists" in out  # resolved


def test_config_show_glob(populated, capsys):
    cli.main(["config", "show", "t*"])
    out = capsys.readouterr().out
    assert "Targets matching t*: 2" in out
    assert "[test1]" in out and "[test2]" in out and "[other]" not in out

    cli.main(["config", "show", "other", "test?"])
    out = capsys.readouterr().out
    assert "Targets matching other, test?: 3" in out

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "show", "x*"])
    assert exc.value.code == 2
    assert "no target matches 'x*'" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        cli.main(["config", "show", "nope"])
    assert "no target named 'nope'" in capsys.readouterr().err


def test_config_show_reports_missing_control_file(populated, tmp_path, capsys):
    (tmp_path / "test1.ctl").unlink()
    cli.main(["config", "show", "test1"])
    assert "(missing)" in capsys.readouterr().out


def test_config_delete_keeps_control_file(populated, tmp_path, capsys):
    cli.main(["config", "delete", "test1"])
    out = capsys.readouterr().out
    assert "Deleted target [test1]" in out and "control file kept" in out
    assert (tmp_path / "test1.ctl").exists()
    assert [t.name for t in load_config(str(populated))] == ["test2", "other"]


def test_config_delete_with_ctl_and_glob(populated, tmp_path, capsys):
    cli.main(["config", "delete", "test*", "--ctl"])
    out = capsys.readouterr().out
    assert "Deleted target [test1] and its control file" in out
    assert "Deleted target [test2] and its control file" in out
    assert not (tmp_path / "test1.ctl").exists()
    assert not (tmp_path / "test2.ctl").exists()
    assert (tmp_path / "other.ctl").exists()
    assert [t.name for t in load_config(str(populated))] == ["other"]

    (tmp_path / "other.ctl").unlink()
    cli.main(["config", "delete", "other", "--ctl"])  # already gone: not an error
    assert "did not exist" in capsys.readouterr().out
    assert load_config(str(populated), allow_empty=True) == []


def test_config_remove_alias(populated, capsys):
    cli.main(["config", "remove", "other"])
    assert "Deleted target [other]" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_config_delete_ctl_requires_config_permission(populated, tmp_path, capsys):
    os.chmod(populated, 0o444)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["config", "delete", "test1", "--ctl"])
        assert exc.value.code == 2
        assert "not permitted to modify" in capsys.readouterr().err
        assert (tmp_path / "test1.ctl").exists()  # control file untouched
        assert [t.name for t in load_config(str(populated))] == ["test1", "test2", "other"]
    finally:
        os.chmod(populated, 0o644)


# --- config location (preferences) -------------------------------------------

def test_user_config_path_resolution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.delenv("FMON_CONFIG", raising=False)
    monkeypatch.setattr(fmon_config, "user_config_path", fmon_config.user_config_path.__wrapped__
                        if hasattr(fmon_config.user_config_path, "__wrapped__") else _real_user_config_path)
    default = home / "fmon" / "fmon.ini"
    assert fmon_config.user_config_path() == str(default)
    fmon_config.set_preference("config_file", str(tmp_path / "elsewhere" / "my.ini"))
    assert fmon_config.user_config_path() == str(tmp_path / "elsewhere" / "my.ini")
    assert (home / "fmon" / "preferences.json").exists()
    monkeypatch.setenv("FMON_CONFIG", str(tmp_path / "env.ini"))      # env wins
    assert fmon_config.user_config_path() == str(tmp_path / "env.ini")


_real_user_config_path = fmon_config.user_config_path


def test_config_move(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.setattr(fmon_config, "user_config_path", _real_user_config_path)
    (tmp_path / "d").mkdir()
    cli.main(["d"])                                   # creates home/fmon/fmon.ini
    assert (home / "fmon" / "fmon.ini").exists()

    new_dir = tmp_path / "newhome"
    cli.main(["config", "move", str(new_dir)])        # a directory -> fmon.ini inside it
    out = capsys.readouterr().out
    assert f"Config file is now {new_dir / 'fmon.ini'} (moved)" in out
    assert not (home / "fmon" / "fmon.ini").exists()
    assert (new_dir / "fmon.ini").exists()
    assert fmon_config.user_config_path() == str(new_dir / "fmon.ini")
    assert [t.name for t in load_config(fmon_config.user_config_path())] == ["d"]
    cli.main(["config", "show", "d"])                 # the CLI follows the new location
    assert str(new_dir / "fmon.ini") in capsys.readouterr().out

    cli.main(["config", "move", str(tmp_path / "third" / "site.ini")])
    assert "(moved)" in capsys.readouterr().out
    assert (tmp_path / "third" / "site.ini").exists()
    cli.main(["config", "move", str(home / "fmon")])  # back to the default: preference removed
    assert "config_file" not in fmon_config.load_preferences()
    assert fmon_config.user_config_path() == str(home / "fmon" / "fmon.ini")
    with pytest.raises(SystemExit):
        cli.main(["config", "-f", "other.ini", "move", "x.ini"])
    assert "cannot be combined with -f" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="Windows ignores the read-only attribute on directories")
@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_config_move_refused_without_permission(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setattr(fmon_config, "user_config_path", _real_user_config_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["config", "move", str(locked / "fmon.ini")])
        assert exc.value.code == 2
        assert "not permitted to create" in capsys.readouterr().err
    finally:
        os.chmod(locked, 0o755)


def test_config_move_creates_file_when_none_exists(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.setattr(fmon_config, "user_config_path", _real_user_config_path)
    target = tmp_path / "fresh" / "fmon.ini"
    cli.main(["config", "move", str(target.parent)])
    assert "(created)" in capsys.readouterr().out
    assert target.exists() and "; fmon per-user configuration" in target.read_text()
    assert fmon_config.user_config_path() == str(target)


def test_obsolete_option_is_reported_and_removable(user_ini, tmp_path, capsys):
    cli.main(["config", "add", "d1", str(tmp_path / "d1")])
    text = user_ini.read_text().replace("control_dir = controls", "control_dir = controls\nlog_backups = 5")
    user_ini.write_text(text)   # as written by an earlier version
    capsys.readouterr()
    cli.main(["config", "show"])           # still works, and warns
    out = capsys.readouterr().out
    assert "[d1]" in out and "Warning: [fmon] log_backups: obsolete" in out
    cli.main(["config", "get"])
    assert "Warning: [fmon] log_backups" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["check"])                # runs (fails only because d1 has no control file yet)
    assert "Control file not found" in capsys.readouterr().err
    cli.main(["config", "unset", "log_backups"])
    assert fmon_config.config_warnings(str(user_ini)) == []
