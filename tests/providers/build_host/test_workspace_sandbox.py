"""workspace.py demotes build subprocesses and hands the workspace to the build user (ADR-0214)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from kdive.profiles.build import GitSourceRef
from kdive.providers.shared.build_host import sandbox as sb
from kdive.providers.shared.build_host.workspaces import workspace as ws
from kdive.security.secrets.secret_registry import SecretRegistry


def _box() -> sb.BuildSandbox:
    return sb.BuildSandbox(uid=9, gid=9, extra_groups=(9,), user_name="b", home="/home/b")


class _R:
    returncode = 0
    stderr = ""
    stdout = "FETCH_HEAD"


def test_write_fragment_owns_file_when_sandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned: list = []
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: owned.append((Path(p), u, g)))
    ws._write_fragment(b"CONFIG_X=y\n", tmp_path, _box())
    frag = tmp_path / "kdump.config.fragment"
    assert frag.read_bytes() == b"CONFIG_X=y\n"
    assert owned == [(frag, 9, 9)]


def test_write_fragment_no_chown_without_sandbox(tmp_path: Path) -> None:
    ws._write_fragment(b"X", tmp_path, None)  # must not raise
    assert (tmp_path / "kdump.config.fragment").read_bytes() == b"X"


def test_sync_tree_adds_chown_under_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    owned: list = []
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/rsync")
    monkeypatch.setattr(ws, "warm_tree_source_error", lambda src: None)
    monkeypatch.setattr(ws.subprocess, "run", lambda argv, **kw: seen.update(argv=argv) or _R())
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: owned.append(Path(p)))
    ws.sync_tree("/warm", tmp_path, sandbox=_box())
    assert "--chown=9:9" in seen["argv"]
    assert owned == [tmp_path]  # dest dir handed to the build user


def test_sync_tree_no_chown_without_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/rsync")
    monkeypatch.setattr(ws, "warm_tree_source_error", lambda src: None)
    monkeypatch.setattr(ws.subprocess, "run", lambda argv, **kw: seen.update(argv=argv) or _R())
    ws.sync_tree("/warm", tmp_path)
    assert not any(a.startswith("--chown") for a in seen["argv"])


def test_clone_tree_owns_empty_dir_before_first_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(sb.os, "chown", lambda p, u, g, **kw: order.append(f"own:{p}"))

    def fake_run_git(args, *, cwd, run_id, sandbox=None):
        order.append(f"git:{args[0]} sandbox={'set' if sandbox is not None else 'none'}")
        return _R()

    monkeypatch.setattr(ws, "_run_git", fake_run_git)
    monkeypatch.setattr(ws, "remote_allowed", lambda r, a: True)
    monkeypatch.setattr(ws, "validate_git_arg", lambda v, n: None)
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/git")
    ws.clone_tree(
        GitSourceRef(remote="https://h/r", ref="main"),
        tmp_path / "run",
        ["h"],
        run_id=uuid.uuid4(),
        secret_registry=SecretRegistry(),
        sandbox=_box(),
    )
    assert order[0].startswith("own:")  # chown the empty dir BEFORE any git call
    assert order[1] == "git:init sandbox=set"
