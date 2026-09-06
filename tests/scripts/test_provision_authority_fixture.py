"""Safety tests for the one disposable private-authority fixture script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

_SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")


def _script() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/live-vm/provision-authority-fixture.py"
    spec = importlib.util.spec_from_file_location("provision_authority_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_private_domain_is_refused_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    closed = False

    class Connection:
        def lookupByName(self, name: str) -> object:
            assert name == f"kdive-{_SYSTEM_ID}"
            return object()

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(script.libvirt, "open", lambda uri: Connection())
    with pytest.raises(ValueError, match="existing private authority fixture domain"):
        script._refuse_existing_authority_domain(_SYSTEM_ID)
    assert closed


def test_main_checks_private_domain_before_worker_or_file_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    mutated = False

    class Provider:
        local_libvirt_section = object()

    class Profile:
        provider = Provider()

    def mutation(*_args: object) -> None:
        nonlocal mutated
        mutated = True

    monkeypatch.setattr(script.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["provision-authority-fixture.py", str(_SYSTEM_ID)])
    monkeypatch.setattr(script.json, "load", lambda _stream: {})
    monkeypatch.setattr(script.ProvisioningProfile, "model_validate", lambda _value: Profile())
    monkeypatch.setattr(
        script,
        "_refuse_existing_authority_domain",
        lambda _system_id: (_ for _ in ()).throw(ValueError("existing private domain")),
    )
    monkeypatch.setattr(script, "_undefine_worker_domain", mutation)
    monkeypatch.setattr(script, "_remove_regular", mutation)
    monkeypatch.setattr(script, "_remove_directory", mutation)

    with pytest.raises(ValueError, match="existing private domain"):
        script.main()
    assert not mutated


def test_baseline_removal_rejects_symlink(tmp_path: Path) -> None:
    script = _script()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "baseline"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="unexpected fixture baseline"):
        script._remove_directory(link)
    assert link.is_symlink()
    assert target.is_dir()
