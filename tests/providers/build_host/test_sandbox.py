"""Build-subprocess privilege-drop primitive + resolution (ADR-0214)."""

from __future__ import annotations

import types

import pytest

import kdive.config as config
from kdive.providers.shared.build_host import sandbox as sb


def _sandbox() -> sb.BuildSandbox:
    return sb.BuildSandbox(
        uid=1000, gid=1000, extra_groups=(1000, 100), user_name="builder", home="/home/builder"
    )


def test_run_assembles_demotion_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    _sandbox().run(["make"], cwd="/ws", check=False)
    kw = captured["kwargs"]
    assert kw["user"] == 1000
    assert kw["group"] == 1000
    assert kw["extra_groups"] == [1000, 100]
    assert kw["umask"] == 0o077
    assert kw["cwd"] == "/ws"


def test_child_env_rebases_identity_onto_build_user() -> None:
    env = _sandbox()._child_env({"HOME": "/root", "USER": "root", "PATH": "/usr/bin"})
    assert env["HOME"] == "/home/builder"
    assert env["USER"] == "builder"
    assert env["LOGNAME"] == "builder"
    assert env["PATH"] == "/usr/bin"  # caller env preserved


def test_child_env_drops_root_xdg_paths() -> None:
    env = _sandbox()._child_env(
        {
            "HOME": "/root",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/0",
            "XDG_CACHE_HOME": "/root/.cache",
        }
    )
    assert "XDG_RUNTIME_DIR" not in env
    assert "XDG_CACHE_HOME" not in env
    assert env["PATH"] == "/usr/bin"


def test_run_layers_build_user_env_over_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sb.subprocess, "run", lambda argv, **kw: captured.update(kw))
    _sandbox().run(["git"], env={"HOME": "/root", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert captured["env"]["HOME"] == "/home/builder"
    assert captured["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"  # hardened env kept


def test_own_chowns_path_to_build_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: calls.append((p, u, g, kw)))
    _sandbox().own("/ws")
    assert calls == [("/ws", 1000, 1000, {"follow_symlinks": False})]


def test_sandbox_run_none_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(sb.subprocess, "run", lambda argv, **kw: captured.update(kw=kw))
    sb.sandbox_run(None, ["make"], check=False)
    assert "user" not in captured["kw"]  # a non-root run must not request a setuid


def _fake_pwnam(name: str):
    return types.SimpleNamespace(pw_name=name, pw_uid=1000, pw_gid=2000, pw_dir="/home/builder")


def test_resolve_non_root_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 1000)
    assert sb._resolve_sandbox() is None


def test_resolve_root_unset_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: None)
    with pytest.raises(sb.CategorizedError) as exc:
        sb._resolve_sandbox()
    assert exc.value.category is sb.ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_BUILD_USER" in str(exc.value)


def test_resolve_root_unknown_account_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "ghost")
    monkeypatch.setattr(sb.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    with pytest.raises(sb.CategorizedError) as exc:
        sb._resolve_sandbox()
    assert exc.value.category is sb.ErrorCategory.CONFIGURATION_ERROR


def test_resolve_root_uid0_account_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "root")
    monkeypatch.setattr(
        sb.pwd,
        "getpwnam",
        lambda n: types.SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0, pw_dir="/root"),
    )
    with pytest.raises(sb.CategorizedError):
        sb._resolve_sandbox()


def test_resolve_root_valid_account_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config, "get", lambda s: "builder")
    monkeypatch.setattr(sb.pwd, "getpwnam", _fake_pwnam)
    monkeypatch.setattr(sb.os, "getgrouplist", lambda n, g: [2000, 100])
    box = sb._resolve_sandbox()
    assert box == sb.BuildSandbox(
        uid=1000, gid=2000, extra_groups=(2000, 100), user_name="builder", home="/home/builder"
    )


def test_provider_memoizes_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def boom() -> sb.BuildSandbox | None:
        calls["n"] += 1
        raise sb.CategorizedError("nope", category=sb.ErrorCategory.CONFIGURATION_ERROR)

    monkeypatch.setattr(sb, "_resolve_sandbox", boom)
    provider = sb.SandboxProvider()
    with pytest.raises(sb.CategorizedError):
        provider.get()
    with pytest.raises(sb.CategorizedError):
        provider.get()
    assert calls["n"] == 1  # resolved once, error re-raised on every call


def test_provider_memoizes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def once() -> sb.BuildSandbox | None:
        calls["n"] += 1
        return None

    monkeypatch.setattr(sb, "_resolve_sandbox", once)
    provider = sb.SandboxProvider()
    assert provider.get() is None
    assert provider.get() is None
    assert calls["n"] == 1
