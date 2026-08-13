import os
from types import SimpleNamespace

from styler import startup


def test_normal_user_is_not_changed(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    result = startup.drop_sudo_root_to_invoking_user()
    assert result.changed is False


def test_sudo_invocation_returns_to_original_user(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setattr(startup.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="lucy", pw_dir="/home/lucy"))
    monkeypatch.setattr(os, "initgroups", lambda name, gid: calls.append(("groups", name, gid)))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))
    result = startup.drop_sudo_root_to_invoking_user()
    assert result.changed is True
    assert os.environ["HOME"] == "/home/lucy"
    assert calls[-2:] == [("gid", 1000), ("uid", 1000)]
