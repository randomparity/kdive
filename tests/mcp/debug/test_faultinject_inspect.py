"""Tests for `faultinject.inspect` (ADR-0825, #3683)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.mcp.tools.debug.faultinject import inspect_faults
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.fault_injection import FAULT_KNOB_PARAMETERS, FaultKnobState
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import ensure_system_bootstrap_key
from tests.mcp.systems_support import TEST_DT as _DT
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import granted_allocation as _granted_allocation
from tests.mcp.systems_support import pool as _pool
from tests.mcp.systems_support import provider_resolver as _provider_resolver
from tests.mcp.systems_support import provisioning_profile as _profile


class _Connector:
    def __init__(self, endpoint: tuple[str, int] | None) -> None:
        self._endpoint = endpoint

    def recorded_ssh_endpoint(
        self, system: object, *, ssh_forward_port: int | None = None
    ) -> tuple[str, int] | None:
        return self._endpoint


class _Inspector:
    def __init__(self, *, raises: CategorizedError | None = None) -> None:
        self._raises = raises
        self.calls: list[tuple[str, str]] = []
        self.key_text: str | None = None
        self.thread: int | None = None

    def read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]:
        self.calls.append((transport_handle, key_path))
        self.key_text = Path(key_path).read_text(encoding="utf-8")
        self.thread = threading.get_ident()
        if self._raises is not None:
            raise self._raises
        slab = dict.fromkeys(FAULT_KNOB_PARAMETERS["failslab"], 0) | {"probability": 5, "times": -1}
        return {
            "failslab": FaultKnobState(present=True, parameters=slab),
            "fail_page_alloc": FaultKnobState(present=False, parameters={}),
        }


def _resolver(
    inspector: _Inspector | None, endpoint: tuple[str, int] | None = ("127.0.0.1", 22022)
) -> ProviderResolver:
    base = _provider_resolver(connector=_Connector(endpoint))
    runtime = dataclasses.replace(
        base.resolve(ResourceKind.LOCAL_LIBVIRT), fault_injection=inspector
    )
    return ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})


async def _seed(pool, state: SystemState = SystemState.READY, *, project: str = "proj") -> str:
    alloc_id = await _granted_allocation(pool)
    sid = uuid4()
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=sid,
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_profile(),
                domain_name=f"kdive-{sid}",
            ),
        )
        await ensure_system_bootstrap_key(conn, system.id, secret_registry=SecretRegistry())
    return str(sid)


def _run(url: str, resolver: ProviderResolver, *, state=SystemState.READY, role=Role.VIEWER):
    async def go():
        async with _pool(url) as pool:
            sid = await _seed(pool, state)
            return await inspect_faults(
                pool,
                _ctx(role=role),
                sid,
                resolver=resolver,
                secret_registry=SecretRegistry(),
            )

    return asyncio.run(go())


def test_viewer_reads_both_knobs_and_the_key_file_is_removed(migrated_url: str) -> None:
    inspector = _Inspector()
    resp = _run(migrated_url, _resolver(inspector))
    assert resp.status == "ok"
    assert resp.data["knobs"] == {
        "failslab": {
            "present": True,
            "armed": True,
            "probability": 5,
            "interval": 0,
            "times": -1,
            "space": 0,
            "verbose": 0,
        },
        "fail_page_alloc": {"present": False, "armed": False},
    }
    assert resp.suggested_next_actions == ["faultinject.reset", "systems.get"]
    handle, key_path = inspector.calls[0]
    assert "127.0.0.1" in handle
    assert inspector.key_text and "PRIVATE KEY" in inspector.key_text
    assert not os.path.exists(key_path)
    assert inspector.thread != threading.get_ident()


def test_invalid_system_id_is_rejected(migrated_url: str) -> None:
    async def go():
        async with _pool(migrated_url) as pool:
            return await inspect_faults(
                pool,
                _ctx(),
                "not-a-uuid",
                resolver=_resolver(_Inspector()),
                secret_registry=SecretRegistry(),
            )

    assert asyncio.run(go()).error_category == ErrorCategory.CONFIGURATION_ERROR.value


def test_unknown_system_and_foreign_project_are_not_found(migrated_url: str) -> None:
    async def go():
        async with _pool(migrated_url) as pool:
            sid = await _seed(pool)
            unknown = await inspect_faults(
                pool,
                _ctx(),
                str(uuid4()),
                resolver=_resolver(_Inspector()),
                secret_registry=SecretRegistry(),
            )
            foreign = await inspect_faults(
                pool,
                _ctx(projects=("other",)),
                sid,
                resolver=_resolver(_Inspector()),
                secret_registry=SecretRegistry(),
            )
            return unknown, foreign

    unknown, foreign = asyncio.run(go())
    assert unknown.error_category == foreign.error_category == ErrorCategory.NOT_FOUND.value


def test_caller_without_a_role_is_denied(migrated_url: str) -> None:
    with pytest.raises(AuthorizationError):
        _run(migrated_url, _resolver(_Inspector()), role=None)


def test_system_that_is_not_ready_is_a_readiness_failure(migrated_url: str) -> None:
    inspector = _Inspector()
    resp = _run(migrated_url, _resolver(inspector), state=SystemState.PROVISIONING)
    assert resp.error_category == ErrorCategory.READINESS_FAILURE.value
    assert inspector.calls == []


def test_provider_without_the_port_is_capability_unsupported(migrated_url: str) -> None:
    resp = _run(migrated_url, _resolver(None))
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.data["reason"] == "capability_unsupported"
    assert resp.data["capability"] == "fault_injection"
    assert resp.data["supported"] == ["local-libvirt"]


def test_missing_ssh_forward_is_ssh_not_provisioned(migrated_url: str) -> None:
    inspector = _Inspector()
    resp = _run(migrated_url, _resolver(inspector, endpoint=None))
    assert resp.data["reason"] == "ssh_not_provisioned"
    assert inspector.calls == []


@pytest.mark.parametrize(
    ("category", "reason"),
    [
        (ErrorCategory.CONFIGURATION_ERROR, "debugfs_not_mounted"),
        (ErrorCategory.CONFIGURATION_ERROR, "fault_injection_unsupported"),
        (ErrorCategory.CONFIGURATION_ERROR, "helper_not_installed"),
        (ErrorCategory.TRANSPORT_FAILURE, "connection_refused"),
        (ErrorCategory.INFRASTRUCTURE_FAILURE, "helper_output_invalid"),
    ],
)
def test_port_errors_keep_their_category_and_reason(
    migrated_url: str, category: ErrorCategory, reason: str
) -> None:
    error = CategorizedError("remediation text", category=category, details={"reason": reason})
    resp = _run(migrated_url, _resolver(_Inspector(raises=error)))
    assert resp.error_category == category.value
    assert resp.data["reason"] == reason
