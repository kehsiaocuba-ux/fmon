"""Shared test setup: never touch the developer's real fmon config or log."""
import pytest
from fmon import config as fmon_config


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, tmp_path_factory, monkeypatch):
    # a separate directory, so tests that snapshot tmp_path itself don't see it
    home = tmp_path_factory.mktemp("userhome")
    path = home / "fmon" / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(path))
    # subprocesses (GUI/scheduler) resolve the path themselves from the environment
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.chdir(tmp_path)
