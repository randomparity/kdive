"""Unit tests for native authority carrier inputs; these are not native evidence."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.live_vm.installed_local_authority_support import (
    CONFIG_ENV,
    NativeAuthorityConfig,
    OwnedResource,
    ResourceLedger,
    load_config,
    require_fault_barrier,
)


def _document(tmp_path: Path) -> Path:
    path = tmp_path / "carrier.json"
    path.write_text(
        json.dumps(
            {
                "installed_revision": "1" * 40,
                "system_id": str(uuid4()),
                "ownership_prefix": "kdive-2151-" + "1" * 12 + "-" + "2" * 8,
                "authority_service": "kdive-external-boot-authority.service",
                "barrier_socket": "/run/kdive/provider-authority/test-barrier.sock",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_absent_trigger_is_the_only_skip_state() -> None:
    assert load_config({}) is None


def test_complete_owner_only_config_loads(tmp_path: Path) -> None:
    path = _document(tmp_path)
    config = load_config({CONFIG_ENV: str(path)})
    assert config is not None
    assert config.ownership_prefix.startswith("kdive-2151-")


def test_set_trigger_with_unsafe_or_partial_config_fails(tmp_path: Path) -> None:
    path = _document(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe metadata"):
        load_config({CONFIG_ENV: str(path)})
    path.chmod(0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["system_id"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config({CONFIG_ENV: str(path)})


def test_ledger_rejects_unowned_and_cleans_exact_reverse_order() -> None:
    prefix = "kdive-2151-" + "1" * 12 + "-" + "2" * 8
    ledger = ResourceLedger(prefix)
    domain = OwnedResource(kind="domain", identity=f"{prefix}-domain")
    volume = OwnedResource(kind="volume", identity=f"{prefix}-volume")
    ledger.record(domain)
    ledger.record(volume)
    with pytest.raises(ValueError, match="outside"):
        ledger.record(OwnedResource(kind="domain", identity="unrelated-domain"))
    removed: list[OwnedResource] = []
    ledger.cleanup(removed.append)
    assert removed == [volume, domain]


def test_missing_fault_barrier_fails_loud(tmp_path: Path) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=tmp_path / "absent.sock",
    )
    with pytest.raises(RuntimeError, match="no deterministic provider-effect barrier"):
        require_fault_barrier(config)
