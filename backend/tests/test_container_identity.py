"""Entrypoint contracts; real privilege dropping is covered by Docker checks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.py"
spec = importlib.util.spec_from_file_location("container_entrypoint", SCRIPT)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


@pytest.mark.parametrize("env,expected", [
    ({}, (None, None)), ({"PUID": "", "PGID": "", "UMASK": ""}, (None, None)),
    ({"UMASK": "002"}, (None, 2)), ({"UMASK": "0002"}, (None, 2)),
    ({"UMASK": "777"}, (None, 511)), ({"UMASK": "0"}, (None, 0)),
    ({"PUID": "00099", "PGID": "100"}, ((99, 100), None)),
    ({"PUID": "4294967294", "PGID": "0"}, ((4294967294, 0), None)),
])
def test_valid_options(env, expected):
    assert entrypoint.options(env) == expected


@pytest.mark.parametrize("env", [
    {"PUID": "99"}, {"PGID": "100"}, {"PUID": "99", "PGID": ""},
    *({"PUID": value, "PGID": "100"} for value in
      ("-1", "+1", " 99", "1.0", "１２", "4294967295", "9" * 100)),
    {"PUID": "99", "PGID": "bad"},
    *({"UMASK": value} for value in ("-1", " 002", "0o22", "888", "1000", "7777", "00000", "secret\nvalue")),
])
def test_invalid_options_are_rejected_without_echoing_values(env, capsys, monkeypatch):
    execute = Mock()
    prepare = Mock()
    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    monkeypatch.setattr(entrypoint, "prepare_data_dir", prepare)
    assert entrypoint.main(["command"], env) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("pullarr-entrypoint:") and output.err.count("\n") == 1
    assert "secret" not in output.err
    execute.assert_not_called()
    prepare.assert_not_called()


def test_no_identity_does_not_touch_ownership_umask_or_groups(monkeypatch):
    execute, prepare, mask = Mock(), Mock(), Mock()
    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    monkeypatch.setattr(entrypoint.os, "umask", mask)
    monkeypatch.setattr(entrypoint, "prepare_data_dir", prepare)
    entrypoint.main(["custom", "one argument"], {})
    execute.assert_called_once_with("custom", ["custom", "one argument"])
    prepare.assert_not_called()
    mask.assert_not_called()


def test_umask_only_also_works_for_docker_user(monkeypatch):
    execute, mask, prepare = Mock(), Mock(), Mock()
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    monkeypatch.setattr(entrypoint.os, "umask", mask)
    monkeypatch.setattr(entrypoint, "prepare_data_dir", prepare)
    entrypoint.main(["command"], {"UMASK": "002"})
    mask.assert_called_once_with(2)
    execute.assert_called_once_with("command", ["command"])
    prepare.assert_not_called()


def test_explicit_identity_rejects_docker_user_before_any_mutation(monkeypatch):
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 1000)
    prepare, mask = Mock(), Mock()
    monkeypatch.setattr(entrypoint, "prepare_data_dir", prepare)
    monkeypatch.setattr(entrypoint.os, "umask", mask)
    assert entrypoint.main(["command"], {"PUID": "99", "PGID": "100", "UMASK": "002"}) == 1
    prepare.assert_not_called()
    mask.assert_not_called()


@pytest.mark.parametrize("data,expected", [(None, "/config"), ("", "/config"), ("/state", "/state")])
def test_explicit_identity_limits_privileges_and_preserves_command(monkeypatch, data, expected):
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    execute, prepare = Mock(), Mock()
    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    monkeypatch.setattr(entrypoint, "prepare_data_dir", prepare)
    env = {"PUID": "99", "PGID": "100"}
    if data is not None:
        env["PULLARR_DATA_DIR"] = data
    entrypoint.main(["python", "-m", "pullarr.main"], env)
    prepare.assert_called_once_with(expected, 99, 100)
    command = execute.call_args.args[1]
    assert command[:4] == ["/usr/bin/setpriv", "--reuid=99", "--regid=100", "--clear-groups"]
    for flag in ("--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs"):
        assert flag in command
    assert command[-4:] == ["--", "python", "-m", "pullarr.main"]


@pytest.mark.parametrize("value", ["/", "/config/..", "/app", "/etc/config", "/comics", "/media/config", "/downloads"])
def test_refuses_root_system_and_media_dirs(value):
    with pytest.raises(entrypoint.ConfigurationError):
        entrypoint.prepare_data_dir(value, 99, 100)


def test_symlinked_appdata_is_refused_before_chown(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(entrypoint.ConfigurationError, match="symlinks"):
        entrypoint.prepare_data_dir(str(alias), 99, 100)


def test_ownership_skips_external_links_hardlinks_mounts_and_special_files(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    data = root / "config"
    data.mkdir()
    (data / "sub").mkdir()
    (data / "sub" / "pullarr.db").write_bytes(b"state")
    library = root / "library"
    library.mkdir()
    external = library / "old.cbz"
    external.write_bytes(b"media")
    (data / "symlink").symlink_to(library, target_is_directory=True)
    (data / "file-link").symlink_to(external)
    os.link(external, data / "hardlink")
    os.mkfifo(data / "fifo")
    mount = data / "nested-mount"
    mount.mkdir()
    (mount / "old.cbz").write_bytes(b"mounted library")
    monkeypatch.setattr(entrypoint, "nested_mounts", lambda: {mount})
    changed = []
    monkeypatch.setattr(entrypoint.os, "fchown", lambda fd, uid, gid: changed.append(os.fstat(fd).st_ino))
    entrypoint.prepare_data_dir(str(data), 12345, 23456)
    assert set(changed) == {p.stat().st_ino for p in (data, data / "sub", data / "sub" / "pullarr.db")}
    assert external.read_bytes() == b"media"


def test_ownership_error_stops_startup_without_exposing_paths(monkeypatch, capsys):
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    def fail(*args):
        raise PermissionError("secret path")
    monkeypatch.setattr(entrypoint, "prepare_data_dir", fail)
    execute = Mock()
    monkeypatch.setattr(entrypoint.os, "execvp", execute)
    assert entrypoint.main(["command"], {"PUID": "99", "PGID": "100"}) == 1
    assert "secret path" not in capsys.readouterr().err
    execute.assert_not_called()


def test_real_command_replaces_entrypoint_and_inherits_umask(tmp_path):
    # Verify PID preservation through exec and actual file/directory modes.
    script = """import json, os, pathlib
p=pathlib.Path('new'); p.mkdir(); (p/'file').write_bytes(b'x')
print(json.dumps([os.getpid(), p.stat().st_mode & 0o777, (p/'file').stat().st_mode & 0o777]))
"""
    proc = subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT), sys.executable, "-c", script],
                            cwd=tmp_path, env={**os.environ, "PUID": "", "PGID": "", "UMASK": "002"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output, error = proc.communicate(timeout=10)
    assert proc.returncode == 0 and error == ""
    assert json.loads(output) == [proc.pid, 0o775, 0o664]


@pytest.mark.parametrize("caller,owner", [(0, 99), (0, 0), (99, 99)])
def test_recovery_drops_root_console_before_writing_state(monkeypatch, caller, owner):
    import stat
    from types import SimpleNamespace
    from pullarr import reset_password

    path = Mock()
    path.stat.return_value = SimpleNamespace(st_uid=owner, st_gid=100, st_mode=stat.S_IFREG | 0o600)
    monkeypatch.setattr(reset_password.os, "geteuid", lambda: caller)
    calls = []
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(reset_password.os, name, lambda value, op=name: calls.append((op, value)))
    reset_password._use_login_owner(path)
    assert calls == ([("setgroups", []), ("setgid", 100), ("setuid", 99)] if caller == 0 and owner else [])
    if caller == 0:
        path.stat.assert_called_once_with(follow_symlinks=False)
    else:
        path.stat.assert_not_called()


def test_root_recovery_refuses_symlinked_login(monkeypatch):
    import stat
    from types import SimpleNamespace
    from pullarr import reset_password

    path = Mock()
    path.stat.return_value = SimpleNamespace(st_mode=stat.S_IFLNK)
    monkeypatch.setattr(reset_password.os, "geteuid", lambda: 0)
    with pytest.raises(OSError, match="regular file"):
        reset_password._use_login_owner(path)
