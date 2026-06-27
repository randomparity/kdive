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


def test_write_fragment_does_not_follow_planted_symlink(tmp_path: Path) -> None:
    # A malicious source tree can plant kdump.config.fragment as a symlink to a root-owned target
    # (git checkout runs demoted, so the build user controls the workspace). The root-privileged
    # write must NOT follow it off the workspace (ADR-0214 — else the privilege drop is defeated).
    target = tmp_path / "victim"
    target.write_bytes(b"original")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "kdump.config.fragment").symlink_to(target)
    ws._write_fragment(b"CONFIG_X=y\n", workspace, None)
    frag = workspace / "kdump.config.fragment"
    assert not frag.is_symlink()  # the decoy symlink was removed, not followed
    assert frag.read_bytes() == b"CONFIG_X=y\n"  # a real file was written in its place
    assert target.read_bytes() == b"original"  # the victim was never touched


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


def test_clone_tree_empty_allowlist_message_names_self_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty allowlist (local git lane off, the common fresh-deploy state) points the developer
    # at the self-service alternative, same as the not-allowlisted message (#778).
    monkeypatch.setattr(ws, "validate_git_arg", lambda v, n: None)
    with pytest.raises(Exception, match="build_envs.list") as exc:
        ws.clone_tree(
            GitSourceRef(remote="https://h/r", ref="main"),
            tmp_path / "run",
            [],
            run_id=uuid.uuid4(),
            secret_registry=SecretRegistry(),
        )
    assert "KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST" in str(exc.value)


def test_clone_tree_returns_provenance_userinfo_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker-local git lane returns {remote, ref, resolved_commit} (#778): remote
    # userinfo-stripped, resolved_commit from the FETCH_HEAD rev-parse (_R.stdout).
    monkeypatch.setattr(ws, "_run_git", lambda *a, **k: _R())
    monkeypatch.setattr(ws, "remote_allowed", lambda r, a: True)
    monkeypatch.setattr(ws, "validate_git_arg", lambda v, n: None)
    monkeypatch.setattr(ws.shutil, "which", lambda t: "/usr/bin/git")
    provenance = ws.clone_tree(
        GitSourceRef(remote="https://u:tok@h/r", ref="v6.9"),  # pragma: allowlist secret
        tmp_path / "run",
        ["h"],
        run_id=uuid.uuid4(),
        secret_registry=SecretRegistry(),
    )
    assert provenance is not None
    assert provenance.dump() == {
        "remote": "https://h/r",
        "ref": "v6.9",
        "resolved_commit": "FETCH_HEAD",
    }
