"""Private intent persistence checks for local authority-owned System operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.system_authority import (
    LocalAuthoritySystemError,
    LocalAuthoritySystemProvider,
    LocalAuthoritySystemTopology,
    _Intent,
)

_DIGEST = "sha256:" + "a" * 64


class _Provisioner:
    def provision(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("intent-only tests must not provision")


def _provider(tmp_path: Path) -> LocalAuthoritySystemProvider:
    base = tmp_path / "base.qcow2"
    base.touch()
    return LocalAuthoritySystemProvider(
        provisioner=_Provisioner(),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: base},
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected teardown")),
        allocate_port=lambda: 2200,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )


def _intent() -> _Intent:
    system_id = uuid4()
    return _Intent(
        system_id=system_id,
        allocation_id=uuid4(),
        resource_id=uuid4(),
        authority_instance="authority-a",
        root_identity=_DIGEST,
        bootstrap_identity=_DIGEST,
        operation_digest=_DIGEST,
        deadline=datetime(2026, 9, 6, 0, 15, tzinfo=UTC),
        domain_name=f"kdive-{system_id}",
        overlay=f"/private/overlays/{system_id}.qcow2",
        baseline=f"/private/baseline/{system_id}",
        base="/private/base.qcow2",
        gdb_port=None,
        ssh_port=2200,
        xml_digest=None,
    )


def test_private_intent_is_fsynced_private_and_replay_is_exact(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _intent()

    provider._store_intent(intent)

    path = tmp_path / "intents" / f"{intent.system_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert provider._load_intent(intent.system_id) == intent
    provider._store_intent(intent)

    changed = replace(intent, deadline=intent.deadline + timedelta(seconds=1))
    with pytest.raises(LocalAuthoritySystemError, match="replaced"):
        provider._store_intent(changed)


def test_observation_load_does_not_create_private_intent_root(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    assert provider._load_intent(uuid4()) is None

    assert not (tmp_path / "intents").exists()


def test_private_intent_symlink_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _intent()
    root = tmp_path / "intents"
    root.mkdir(mode=0o700)
    path = root / f"{intent.system_id}.json"
    path.symlink_to(tmp_path / "target")

    with pytest.raises(LocalAuthoritySystemError, match="unsafe"):
        provider._load_intent(intent.system_id)
