import pytest
import os
import json
import stat
from fmon.scanner import FolderScanner

def test_scanner_creates_snapshot(tmp_path):
    d = tmp_path / "sub"
    d.mkdir()
    p = d / "hello.txt"
    p.write_text("world")

    scanner = FolderScanner(str(tmp_path))
    snapshot = scanner.scan()
    keys = [k.replace("\\", "/") for k in snapshot.keys()]
    assert any("hello.txt" in k for k in keys)

def test_save_control_file(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("data")
    ctl = tmp_path / "out.ctl"

    scanner = FolderScanner(str(tmp_path))
    scanner.save_control_file(str(ctl))
    
    assert ctl.exists()
    data = json.loads(ctl.read_text())
    assert len(data) > 0


def _norm(entries):
    """Normalise diff entries (path or (path, ...) tuples) to sorted posix paths."""
    return sorted((e[0] if isinstance(e, tuple) else e).replace("\\", "/") for e in entries)


def _make_tree(root):
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b")
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "c.txt").write_text("c")


def test_scan_is_recursive(tmp_path):
    _make_tree(tmp_path)
    snapshot = FolderScanner(str(tmp_path)).scan()
    assert _norm(snapshot) == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]


def test_check_no_changes(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "no_changes.ctl"
    scanner = FolderScanner(str(tmp_path))
    scanner.save_control_file(str(ctl))

    diff = scanner.check(str(ctl))
    assert diff == {"added": [], "removed": [], "modified": []}


def test_check_detects_changes_in_subfolders(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "changes.ctl"
    scanner = FolderScanner(str(tmp_path))
    scanner.save_control_file(str(ctl))

    # modified (size change) in a nested subfolder
    (tmp_path / "sub" / "deep" / "c.txt").write_text("changed")
    # removed from a subfolder
    (tmp_path / "sub" / "b.txt").unlink()
    # added in a brand new subfolder
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "d.txt").write_text("d")

    diff = scanner.check(str(ctl))
    assert _norm(diff["added"]) == ["new/d.txt"]
    assert _norm(diff["removed"]) == ["sub/b.txt"]
    assert _norm(diff["modified"]) == ["sub/deep/c.txt"]
    _, before, after = diff["modified"][0]
    assert (before["size"], after["size"]) == (1, 7)


def test_check_detects_mtime_only_change(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "mtime.ctl"
    scanner = FolderScanner(str(tmp_path))
    scanner.save_control_file(str(ctl))

    target = tmp_path / "sub" / "b.txt"
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 100))  # same size, new mtime

    diff = scanner.check(str(ctl))
    assert _norm(diff["modified"]) == ["sub/b.txt"]
    assert diff["added"] == [] and diff["removed"] == []


def test_cli_check_exit_codes(tmp_path, capsys, monkeypatch):
    from fmon import cli

    _make_tree(tmp_path)
    ctl = tmp_path.parent / "cli.ctl"

    monkeypatch.setattr("sys.argv", ["fmon", "snapshot", str(tmp_path), "-o", str(ctl)])
    cli.main()
    assert ctl.exists()

    monkeypatch.setattr("sys.argv", ["fmon", "check", str(tmp_path), "-c", str(ctl)])
    cli.main()  # no changes -> returns normally
    assert "No changes detected." in capsys.readouterr().out

    (tmp_path / "sub" / "extra.txt").write_text("x")
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"fmon check: {tmp_path}" in out  # full path as report title
    assert "Added: 1" in out and "extra.txt" in out

    monkeypatch.setattr("sys.argv", ["fmon", "check", str(tmp_path), "-c", str(tmp_path / "missing.ctl")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "Control file not found" in capsys.readouterr().err


def test_scan_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        FolderScanner(str(tmp_path / "nope")).scan()


def test_save_control_file_creates_parent_dir(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "newdir" / "nested" / "out.ctl"
    FolderScanner(str(tmp_path)).save_control_file(str(ctl))
    assert ctl.exists()


def test_scan_exclude(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path / "sub" / "self.ctl"
    ctl.write_text("{}")
    snapshot = FolderScanner(str(tmp_path), exclude=[str(ctl)]).scan()
    assert _norm(snapshot) == ["a.txt", "sub/b.txt", "sub/deep/c.txt"]


# NOTE: Windows only honours the read-only flag in chmod: 0o444 -> read-only,
# anything with the owner-write bit -> 0o666. So tests use 0o666 <-> 0o444
# transitions, which behave identically on POSIX and Windows.


def test_snapshot_records_permissions(tmp_path):
    _make_tree(tmp_path)
    target = tmp_path / "sub" / "b.txt"
    os.chmod(target, 0o444)
    snapshot = FolderScanner(str(tmp_path)).scan()
    key = next(k for k in snapshot if k.endswith("b.txt"))
    assert snapshot[key]["mode"] == 0o444
    assert snapshot[key]["mode"] == stat.S_IMODE(os.stat(target).st_mode)


def test_check_detects_permission_only_change(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "perms.ctl"
    scanner = FolderScanner(str(tmp_path))
    target = tmp_path / "sub" / "b.txt"
    os.chmod(target, 0o666)
    scanner.save_control_file(str(ctl))

    os.chmod(target, 0o444)  # same size, same mtime, new permissions

    diff = scanner.check(str(ctl))
    assert _norm(diff["modified"]) == ["sub/b.txt"]
    _, before, after = diff["modified"][0]
    assert (before["mode"], after["mode"]) == (0o666, 0o444)
    assert before["size"] == after["size"]


def test_check_tolerates_old_control_file_without_mode(tmp_path):
    _make_tree(tmp_path)
    ctl = tmp_path.parent / "old.ctl"
    scanner = FolderScanner(str(tmp_path))
    scanner.save_control_file(str(ctl))
    data = json.loads(ctl.read_text())
    for entry in data.values():
        del entry["mode"]
    ctl.write_text(json.dumps(data))

    diff = scanner.check(str(ctl))
    assert not any(diff.values())


def test_report_shows_before_and_after(tmp_path, capsys):
    from fmon import cli

    _make_tree(tmp_path)
    ctl = tmp_path.parent / "report.ctl"
    scanner = FolderScanner(str(tmp_path))
    target = tmp_path / "sub" / "deep" / "c.txt"
    os.chmod(target, 0o666)
    scanner.save_control_file(str(ctl))

    target.write_text("c" * 10)
    os.chmod(target, 0o444)
    (tmp_path / "a.txt").unlink()

    cli.print_report(str(tmp_path), str(ctl), scanner.check(str(ctl)))
    out = capsys.readouterr().out

    assert out.startswith(f"fmon check: {tmp_path}")
    assert f"Control file: {ctl}" in out
    assert "size:  1 B -> 10 B  *" in out
    assert "perms: 0666 (rw-rw-rw-) -> 0444 (r--r--r--)  *" in out
    assert "Removed: 1" in out and "a.txt  (size 1 B, perms 0" in out
