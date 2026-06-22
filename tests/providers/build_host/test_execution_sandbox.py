"""execution.py run-steps route through the sandbox chokepoint (ADR-0214)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.providers.shared.build_host import execution as ex
from kdive.providers.shared.build_host import sandbox as sb


def _box() -> sb.BuildSandbox:
    return sb.BuildSandbox(uid=7, gid=7, extra_groups=(7,), user_name="b", home="/home/b")


class _R:
    returncode = 0


def test_run_make_passes_sandbox_to_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen["sandbox"] = sandbox
        seen["argv"] = argv
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    assert ex.real_run_make(Path("/ws"), sandbox=box) == 0
    assert seen["sandbox"] is box
    assert seen["argv"][0] == "make"


def test_run_make_default_sandbox_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.setdefault("sandbox", sandbox)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    ex.real_run_make(Path("/ws"))
    assert seen["sandbox"] is None


def test_olddefconfig_threads_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.update(sandbox=sandbox, argv=argv)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    ex.real_run_olddefconfig(Path("/ws"), sandbox=box)
    assert seen["sandbox"] is box
    assert "olddefconfig" in seen["argv"]


def test_modules_install_threads_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_sandbox_run(sandbox, argv, **kwargs):
        seen.update(sandbox=sandbox, argv=argv)
        return _R()

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    box = _box()
    ex.real_run_modules_install(Path("/ws"), Path("/mod"), sandbox=box)
    assert seen["sandbox"] is box
    assert "modules_install" in seen["argv"]
