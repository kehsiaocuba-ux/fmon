import re
import pytest
from fmon import cli, manual


def test_every_command_has_complete_documentation():
    for name, cmd in manual.COMMANDS.items():
        assert cmd["summary"] and cmd["usage"] and cmd["description"].strip(), name
        assert cmd["options"], name
        assert len(cmd["examples"]) >= 3, f"{name} needs examples"
        for example, explanation in cmd["examples"]:
            assert example.startswith("fmon"), example
            assert explanation, example


def test_manual_documents_all_cli_commands_and_options():
    """Every subcommand and every option argparse knows must appear in the manual."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, __import__("argparse")._SubParsersAction))
    text = manual.render_manual()
    for name, subparser in sub.choices.items():
        assert name in manual.COMMANDS, f"command {name} is undocumented"
        for action in subparser._actions:
            if action.help == __import__("argparse").SUPPRESS or action.dest == "help":
                continue
            flags = [f for f in action.option_strings if f.startswith("--")] or action.option_strings
            if flags:
                assert any(f in text for f in flags), f"{name}: option {flags} undocumented"
    # config actions
    cfg = sub.choices["config"]
    actions = next(a for a in cfg._actions if isinstance(a, __import__("argparse")._SubParsersAction))
    for action_name in actions.choices:
        assert re.search(rf"\b{action_name}\b", text), f"config {action_name} undocumented"


def test_manual_width_and_examples():
    text = manual.render_manual()
    assert max(len(l) for l in text.splitlines()) <= 100
    assert text.count("$ fmon") >= 40
    for needle in ('fmon config show "t*"', "fmon config update log_max_size 5M", "fmon config move",
                   "fmon config delete", "--ctl", "fmon -c test1.ctl", "fmon log --files", "log_keep_days",
                   "%APPDATA%", "Warning", "EXIT STATUS"):
        assert needle in text, needle


def test_render_single_command():
    text = manual.render_command("config")
    assert text.startswith("FMON CONFIG")
    for section in ("NAME", "SYNOPSIS", "DESCRIPTION", "OPTIONS", "EXAMPLES"):
        assert section in text
    assert "fmon config set|update OPTION VALUE" in text
    assert "SNAPSHOT" not in text


def test_help_command(capsys):
    cli.main(["help"])
    assert capsys.readouterr().out.startswith("FMON(1)")
    cli.main(["help", "check"])
    out = capsys.readouterr().out
    assert out.startswith("FMON CHECK") and "$ fmon -c test1.ctl" in out
    with pytest.raises(SystemExit) as exc:
        cli.main(["help", "bogus"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_command_help_includes_description_and_examples(capsys):
    for name in ("snapshot", "check", "config", "log"):
        with pytest.raises(SystemExit) as exc:
            cli.main([name, "-h"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "examples:" in out, name
        assert manual.COMMANDS[name]["examples"][0][0] in out, name
        assert max(len(l) for l in out.splitlines()) <= 100


def test_main_help_points_to_manual(capsys):
    with pytest.raises(SystemExit):
        cli.main(["-h"])
    out = capsys.readouterr().out
    assert "fmon help" in out and "examples:" in out and "config set" in out


def test_help_is_not_logged(tmp_path, monkeypatch):
    from fmon import config as fmon_config
    ini = tmp_path / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(ini))
    cli.main([])
    cli.main(["help", "log"])
    assert not (tmp_path / "fmon.log").exists()


def test_page_without_tty_prints_plainly(capsys, monkeypatch):
    manual.page("hello")
    assert capsys.readouterr().out == "hello\n"
