"""GUI tests. They need Tk and a display (on Linux: run under xvfb-run);
they are skipped automatically otherwise.

NOTE: real windows appear while these tests run (about a minute). Do not
click or type in them - closing one aborts the test that owns it."""
import os
import time
import pytest

tk = pytest.importorskip("tkinter")
from fmon import config as fmon_config
from fmon import log as fmon_log
from fmon.config import LogSettings, load_config

try:
    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except tk.TclError:
    HAVE_DISPLAY = False

pytestmark = pytest.mark.skipif(not HAVE_DISPLAY, reason="no display for Tk")

from fmon import gui  # noqa: E402

_REAL_USER_CONFIG_PATH = fmon_config.user_config_path   # before conftest patches it


@pytest.fixture
def env(tmp_path, monkeypatch):
    ini = tmp_path / "home" / "fmon" / "fmon.ini"
    monkeypatch.setattr(fmon_config, "user_config_path", lambda: str(ini))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home"))   # for the subprocesses
    monkeypatch.setenv("APPDATA", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    for name in ("web", "docs"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.txt").write_text(name)
    return ini


def make_app(env, **kwargs):
    application = gui.App(config_path=str(env), auto_start_scheduler=False, **kwargs)
    # a test window must not be closed by the user while the test is running
    application.protocol("WM_DELETE_WINDOW", lambda: None)
    application.geometry("+0+0")
    application.lower()
    application.update()
    return application


def close_app(application):
    try:
        application.close()
    except tk.TclError:
        pass  # already destroyed


@pytest.fixture
def app(env):
    application = make_app(env)
    yield application
    close_app(application)


def alive(app):
    try:
        return bool(app.winfo_exists())
    except tk.TclError:
        return False


def pump(app, until=lambda: True, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        assert alive(app), "the fmon window was destroyed during the test (was it closed by hand?)"
        if until():
            app.update()
            return True
        time.sleep(0.05)
    return False


def wait_idle(app):
    assert pump(app, until=lambda: app.pending == 0), "commands still running"


def run(app, args):
    done = {}
    app.run_command(args, on_done=lambda p: done.setdefault("proc", p))
    assert pump(app, until=lambda: "proc" in done)
    return done["proc"]


def console_text(app):
    return app.console.text.get("1.0", "end")


def test_app_starts_without_config(app, env):
    assert not env.exists()
    assert app.targets_tab.table.get_children() == ()
    assert "not found" in app.settings_tab.writable_label.cget("text")
    assert app.log_tab.range_label.cget("text").startswith("0 of 0")


def test_snapshot_and_check_via_commands(app, env, tmp_path):
    proc = run(app, ["web", "-n", "website"])
    assert proc.returncode == 0, proc.stderr
    assert env.exists()
    pump(app, until=lambda: app.targets_tab.table.exists("website"))
    app.targets_tab.refresh()
    assert app.targets_tab.table.set("website", "status") == "exists"
    assert "$ fmon web -n website" in console_text(app)
    assert "Snapshot of" in console_text(app)

    (tmp_path / "web" / "new.txt").write_text("!")
    app.targets_tab.table.selection_set("website")
    app.targets_tab.check()
    wait_idle(app)
    text = console_text(app)
    assert "$ fmon check -t website" in text and "new.txt" in text and "[exit 1]" in text
    # the GUI command is in the audit log, tagged [gui], with the report
    log = (env.parent / "fmon.log").read_text()
    assert "[gui] fmon check -t website" in log and "new.txt" in log
    app.log_tab.refresh()
    assert app.log_tab.pager.total >= 2


def test_targets_filter(app, env, tmp_path):
    run(app, ["web"]); run(app, ["docs"])
    app.targets_tab.refresh()
    assert set(app.targets_tab.table.get_children()) == {"web", "docs"}
    app.targets_tab.filter.set("w*")
    assert app.targets_tab.table.get_children() == ("web",)
    app.targets_tab.filter.set("doc")
    assert app.targets_tab.table.get_children() == ("docs",)
    app.targets_tab.filter.set("")
    assert len(app.targets_tab.table.get_children()) == 2


def test_run_tab_command_line(app, env):
    run(app, ["web"])
    app.run_tab.command.set("fmon config show web")
    app.run_tab.run()
    wait_idle(app)
    text = console_text(app)
    assert "$ fmon config show web" in text and "Targets matching web: 1" in text
    assert "fmon config show web" in app.run_tab.history
    app.run_tab.run("help")
    wait_idle(app)
    assert "FMON(1)" in console_text(app)


def test_add_edit_delete_target_through_config_commands(app, env, tmp_path):
    # what the Add dialog produces
    app.run_commands([["config", "add", "docs", str(tmp_path / "docs")],
                      ["config", "set", "docs", "schedule", "15m"]],
                     on_done=lambda ok: app.after_config_change())
    wait_idle(app)
    pump(app, until=lambda: app.targets_tab.table.exists("docs"))
    (t,) = load_config(str(env))
    assert t.schedule == 900
    assert app.targets_tab.table.set("docs", "schedule") == "15m"
    assert app.schedule_tab.table.exists("docs")
    # edit: change schedule off and control file
    app.run_commands([["config", "unset", "docs", "schedule"],
                      ["config", "set", "docs", "control_file", str(tmp_path / "d.ctl")]],
                     on_done=lambda ok: app.after_config_change())
    wait_idle(app)
    (t,) = load_config(str(env))
    assert t.schedule is None and t.control_file == str(tmp_path / "d.ctl")
    # delete with --ctl
    run(app, ["snapshot", "-t", "docs"])
    assert (tmp_path / "d.ctl").exists()
    run(app, ["config", "delete", "docs", "--ctl"])
    app.after_config_change()
    assert not (tmp_path / "d.ctl").exists()
    assert load_config(str(env), allow_empty=True) == []
    assert app.targets_tab.table.get_children() == ()


def test_settings_tab_save(app, env):
    run(app, ["config", "init"])
    app.settings_tab.refresh()
    tab = app.settings_tab
    assert "Writable by you: yes" in tab.writable_label.cget("text")
    # boxes always show the values in effect, even defaults
    assert tab.vars["log_max_size"].get() == "1M" and tab.origin["log_max_size"].cget("text") == "(default)"
    assert tab.vars["log_keep_days"].get() == "0"
    assert tab.vars["log_file"].get() == str(env.parent / "fmon.log")
    assert tab.vars["control_dir"].get() == str(env.parent / "controls")     # full path, as used
    assert tab.origin["control_dir"].cget("text") == "(set in config)"
    tab.vars["log_max_size"].set("5M")
    tab.vars["log_keep_days"].set("60")
    tab.save()
    wait_idle(app)
    settings = fmon_config.log_settings(str(env))
    assert settings.max_size == 5 * 1024 ** 2 and settings.keep_days == 60
    assert tab.vars["log_max_size"].get() == "5M" and tab.origin["log_max_size"].cget("text") == "(set in config)"
    assert tab.vars["log_keep_days"].get() == "60"           # still shown after saving
    tab.vars["log_max_size"].set("")                          # clear -> back to default (unset)
    tab.save()
    wait_idle(app)
    assert fmon_config.log_settings(str(env)).max_size == fmon_config.DEFAULT_LOG_MAX_SIZE
    assert tab.vars["log_max_size"].get() == "1M" and tab.origin["log_max_size"].cget("text") == "(default)"
    tab.save()                                                # nothing changed -> no command
    assert app.status_var.get() == "No changes."


@pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root ignores file permissions")
def test_readonly_config_is_reported(app, env, monkeypatch):
    run(app, ["web"])
    os.chmod(env, 0o444)
    try:
        shown = {}
        monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: shown.setdefault("msg", a))
        app.settings_tab.refresh()
        assert "read-only" in app.settings_tab.writable_label.cget("text")
        run(app, ["config", "set", "log_keep_days", "3"])
        assert "msg" in shown and "not permitted" in shown["msg"][1]
    finally:
        os.chmod(env, 0o644)


def test_log_tab_paging_and_filters(app, env):
    log = env.parent / "fmon.log"
    env.parent.mkdir(parents=True, exist_ok=True)
    settings = LogSettings(path=str(log), max_size=0, keep_days=0)
    from datetime import datetime
    for i in range(130):
        level = ("Info", "Warning", "Error")[i % 3]
        fmon_log.write_entry(settings, level, f"fmon cmd{i}", f"result {i}",
                             user=("alice", "bob")[i % 2], when=datetime(2026, 11, 1 + i // 100, i % 24, 0, 0),
                             details="Added: 1\n  file%d.txt" % i if level == "Info" else None)
    tab = app.log_tab
    tab.refresh()
    assert tab.pager.total == 130
    assert tab.range_label.cget("text").startswith("1 - 25 of 130")
    assert len(tab.table.get_children()) == 25
    assert tab.table.item("0", "values")[3] == "fmon cmd129"        # newest first
    tab.move("next")
    assert tab.range_label.cget("text").startswith("26 - 50 of 130")
    tab.move("last")
    assert tab.range_label.cget("text").startswith("126 - 130 of 130")
    assert len(tab.table.get_children()) == 5
    tab.page_size.set("100"); tab.set_page_size()
    assert tab.pager.page_size == 100 and tab.range_label.cget("text").startswith("101 - 130 of 130")
    tab.goto_var.set("1"); tab.goto()
    assert tab.range_label.cget("text").startswith("1 - 100 of 130")
    # filters
    tab.level.set("Error"); tab.apply()
    assert tab.pager.total == 43 and all(v[0] == "Error" for v in (tab.table.item(i, "values") for i in tab.table.get_children()))
    tab.user.set("bob"); tab.apply()
    assert tab.pager.total == 21
    tab.reset()
    assert tab.pager.total == 130 and tab.level.get() == "All"
    tab.text.set("file6.txt"); tab.apply()   # searched inside the report of an Info entry
    assert tab.pager.total == 1
    tab.reset(); tab.command.set("cmd12"); tab.apply()
    assert tab.pager.total == 11
    tab.reset(); tab.since.set("2026-11-02"); tab.apply()
    assert tab.pager.total == 30
    tab.until.set("2026-11-02 05:00"); tab.apply()
    assert tab.pager.total == 8   # day-2 entries at hours 0-5
    # details of the selected entry
    tab.reset()
    tab.table.selection_set("0"); tab.show_details()
    details = tab.details.get("1.0", "end")
    assert "fmon cmd129" in details and "[file: fmon.log]" in details
    assert "bob" in tab.user_box.cget("values")


def test_log_tab_bad_date(app, env, monkeypatch):
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    app.log_tab.since.set("yesterday")
    app.log_tab.apply()
    assert errors and "invalid date" in errors[0][1]


def test_scheduler_in_gui(app, env, tmp_path):
    run(app, ["web"])
    run(app, ["config", "set", "web", "schedule", "5s"])
    app.after_config_change()
    assert app.schedule_tab.table.exists("web")
    assert app.schedule_tab.table.set("web", "next") == "scheduler stopped"
    app.start_scheduler()
    assert app.scheduler.running
    assert "Scheduler running" in app.schedule_tab.status.cget("text")
    (tmp_path / "web" / "g.txt").write_text("x")
    app.scheduler.run_now("web")
    assert pump(app, until=lambda: app.schedule_tab.table.exists("web")
                and app.schedule_tab.table.set("web", "result") == "CHANGES", timeout=20)
    assert "1 added" in app.schedule_tab.table.set("web", "summary")
    assert "[scheduled" in console_text(app) and "g.txt" in console_text(app)
    assert app.targets_tab.table.set("web", "last").endswith("CHANGES")
    log = (env.parent / "fmon.log").read_text()
    assert "[scheduled] fmon check -t web" in log
    app.stop_scheduler()
    assert pump(app, until=lambda: "Scheduler stopped" in app.schedule_tab.status.cget("text"), timeout=10)


def test_switch_config_file(app, env, tmp_path):
    other = tmp_path / "other.ini"
    other.write_text(f"[x]\npath = {tmp_path / 'docs'}\n")
    app.set_config_path(str(other))
    assert app.config_path == str(other)
    assert app.targets_tab.table.get_children() == ("x",)
    assert str(other) in app.title()


def test_schedule_tab_lists_all_targets(app, env):
    run(app, ["web"]); run(app, ["docs"])
    run(app, ["config", "set", "web", "schedule", "15m"])
    app.after_config_change()
    rows = {iid: app.schedule_tab.table.item(iid, "values") for iid in app.schedule_tab.table.get_children()}
    assert set(rows) == {"web", "docs"}
    assert rows["web"][1] == "15m" and rows["web"][5] == "scheduler stopped"
    assert rows["docs"][1] == "off" and rows["docs"][5] == "not scheduled"
    # "Set schedule..." acts on the selection; simulate its effect for an unscheduled target
    app.schedule_tab.table.selection_set("docs")
    app.run_commands([["config", "set", "docs", "schedule", "1h"]], on_done=lambda ok: app.after_config_change())
    wait_idle(app)
    assert app.schedule_tab.table.set("docs", "interval") == "1h"
    # "Run now" on an unscheduled target runs a plain check
    run(app, ["config", "unset", "docs", "schedule"]); app.after_config_change()
    app.schedule_tab.table.selection_set("docs")
    app.schedule_tab.run_now()
    wait_idle(app)
    assert "$ fmon check -t docs" in console_text(app)


def test_console_hidden_on_log_and_help_tabs(app):
    panes = lambda: [str(p) for p in app.paned.panes()]
    app.show_tab("Log"); app.update()
    assert not app.console_shown and str(app.console) not in panes()
    app.show_tab("Help"); app.update()
    assert not app.console_shown
    app.show_tab("Run"); app.update()
    assert app.console_shown and str(app.console) in panes()


def test_layout_is_persisted_per_tab(env, tmp_path):
    prefs_file = fmon_config.preferences_path()
    application = make_app(env)
    try:
        application.geometry("1000x700")
        for _ in range(5):
            application.update(); time.sleep(0.05)
        application.show_tab("Run"); application.update()
        application.paned.sash_place(0, 0, 333)
        application.save_sash("sash_Run", application.paned)
        application.show_tab("Targets"); application.update()
        application.paned.sash_place(0, 0, 444)
        application.save_sash("sash_Targets", application.paned)
        application.update()
    finally:
        close_app(application)
    prefs = fmon_config.load_preferences()
    assert prefs["sash_Run"] == 333 and prefs["sash_Targets"] == 444
    assert prefs["geometry"].startswith("1000x700")
    assert os.path.isfile(prefs_file)

    again = make_app(env)
    try:
        for _ in range(6):
            again.update(); time.sleep(0.05)
        assert again.paned.sash_coord(0)[1] == 444              # Targets tab restored
        again.show_tab("Run")
        for _ in range(3):
            again.update(); time.sleep(0.05)
        assert again.paned.sash_coord(0)[1] == 333              # Run tab restored
        again.reset_layout()
        assert "sash_Run" not in again.prefs
    finally:
        close_app(again)


def test_appearance_applies_and_persists(app):
    app.apply_appearance(palette="Dark", family="", size=12)
    assert app.appearance.palette_name == "Dark" and app.appearance.font_size == 12
    assert app.console.text.cget("bg") == gui.PALETTES["Dark"]["console_bg"]
    assert app.settings_tab.palette.get() == "Dark" and app.settings_tab.font_size.get() == "12"
    prefs = fmon_config.load_preferences()
    assert prefs["palette"] == "Dark" and prefs["font_size"] == 12
    app.bump_font(1)
    assert app.appearance.font_size == 13
    app.settings_tab.reset_appearance()
    assert app.appearance.palette_name == "Light" and app.appearance.font_size == 10
    assert fmon_config.load_preferences()["palette"] == "Light"


def test_help_tab_and_about(app, monkeypatch):
    tab = app.help_tab
    text = tab.text.get("1.0", "end")
    assert text.startswith("FMON(1)") and "CONFIGURATION FILE" in text
    assert "SNAPSHOT" in tab.sections and "EXIT STATUS" in tab.sections
    tab.section.set("EXIT STATUS"); tab.goto_section()
    tab.query.set("log_keep_days"); tab.find()
    assert "match(es)" in tab.hits.cget("text") and tab.text.tag_ranges("hit")
    # About is a themed dialog (not a native message box), so it follows the color scheme.
    # Build it directly (app.about() would block in the modal loop) and inspect it.
    app.apply_appearance(palette="Dark")
    dialog = gui.AboutDialog(app)
    try:
        dialog.update()
        texts = [w.cget("text") for f in dialog.winfo_children() for w in f.winfo_children()
                 if isinstance(w, gui.ttk.Label)]
        assert any("MIT license" in t for t in texts)
        assert any("Alejandro Castaneda" in t for t in texts)
        assert any(t.startswith("fmon 2.") for t in texts)
        assert dialog.cget("bg") == gui.PALETTES["Dark"]["bg"]
        buttons = [w.cget("text") for f in dialog.winfo_children() for w in f.winfo_children()
                   if isinstance(w, gui.ttk.Button)]
        assert buttons == ["OK"]
    finally:
        dialog.ok()
    assert not any(isinstance(w, gui.AboutDialog) for w in app.winfo_children())


def test_settings_tab_relocates_config(app, env, tmp_path, monkeypatch):
    # use the real resolution (env var -> preferences -> default) so the CLI would follow
    monkeypatch.setattr(fmon_config, "user_config_path", _REAL_USER_CONFIG_PATH)
    assert fmon_config.user_config_path() == str(env)
    run(app, ["web"])
    assert env.exists()
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    new_dir = tmp_path / "mycfg"
    app.settings_tab.ini_dir.set(str(new_dir))
    app.settings_tab.ini_name.set("site.ini")
    app.settings_tab.apply_location()
    new_path = new_dir / "site.ini"
    assert new_path.exists() and not env.exists()                    # moved
    assert app.config_path == str(new_path)
    assert fmon_config.user_config_path() == str(new_path)           # CLI follows
    assert app.targets_tab.table.exists("web")
    assert "Custom location" in app.settings_tab.location_note.cget("text")
    # back to the default location
    app.settings_tab._default_location()
    app.settings_tab.apply_location()
    assert env.exists() and not new_path.exists()
    assert "config_file" not in fmon_config.load_preferences()


def test_settings_browse_fields_exist(app):
    body = app.settings_tab.scroller.inner
    settings_box = next(w for w in body.winfo_children()
                        if isinstance(w, gui.ttk.LabelFrame) and w.cget("text") == "[fmon] settings")
    def buttons(widget):
        for w in widget.winfo_children():
            if isinstance(w, gui.ttk.Button):
                yield w.cget("text")
            yield from buttons(w)
    labels = list(buttons(settings_box))
    assert labels.count("Browse...") == 2 and "Save" in labels and "Reload" in labels


def test_config_error_is_shown_in_targets_and_settings(app, env):
    run(app, ["web"])
    app.refresh_all()
    assert app.targets_tab.table.exists("web") and not app.targets_tab.banner.winfo_ismapped()
    env.write_text(env.read_text().replace("[web]", "[web]\nbogus = 1"))
    app.refresh_all()
    assert app.targets_tab.table.get_children() == ()
    app.update()
    assert app.targets_tab.banner.winfo_ismapped()
    assert "unknown option(s) in [web]: bogus" in app.targets_tab.banner.cget("text")
    assert "cannot be loaded" in app.settings_tab.problem_label.cget("text")
    assert app.status_var.get().startswith("Config error")
    # settings boxes still show values (not wiped) and the raw contents are there to fix the file
    assert app.settings_tab.vars["control_dir"].get()
    assert "bogus = 1" in app.settings_tab.raw.get("1.0", "end")
    env.write_text(env.read_text().replace("\nbogus = 1", ""))
    app.refresh_all(); app.update()
    assert app.targets_tab.table.exists("web") and not app.targets_tab.banner.winfo_ismapped()


def test_obsolete_option_warning_and_fix(app, env):
    run(app, ["web"])
    env.write_text(env.read_text().replace("control_dir = controls", "control_dir = controls\nlog_backups = 5"))
    app.refresh_all(); app.update()
    assert app.targets_tab.table.exists("web")                      # still usable
    assert "log_backups" in app.settings_tab.problem_label.cget("text")
    assert app.settings_tab.fix_button.winfo_ismapped()
    app.settings_tab.fix_obsolete()
    wait_idle(app)
    assert "log_backups" not in env.read_text()
    app.update()
    assert not app.settings_tab.problems.winfo_ismapped()


def test_settings_tab_scrolls_and_texts_wrap(app):
    tab = app.settings_tab
    assert isinstance(tab.scroller, gui.ScrollableFrame)
    assert tab.scroller.canvas.cget("yscrollcommand")
    assert app.log_tab.details.cget("wrap") == "word"
    assert app.console.text.cget("wrap") == "word"
    assert app.paned.cget("showhandle") in (1, "1", True)
    assert app.paned.cget("sashcursor") == "sb_v_double_arrow"   # only over the sash
    assert app.paned.cget("cursor") == ""                          # the rest keeps the normal pointer
