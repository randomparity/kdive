"""Tests for `faultinject.set` and `faultinject.reset` (ADR-0826, #3684)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.mcp.tools.debug import faultinject as faultinject_module
from kdive.mcp.tools.debug.faultinject import inspect_faults, reset_faults, set_fault
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.fault_injection import FAULT_KNOB_PARAMETERS, FaultKnobState
from kdive.security.audit import args_digest
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import ensure_system_bootstrap_key
from kdive.services.external_boot.admission import ExternalBootDenied, ExternalBootOperation
from tests.mcp.systems_support import TEST_DT as _DT
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import granted_allocation as _granted_allocation
from tests.mcp.systems_support import pool as _pool
from tests.mcp.systems_support import provider_resolver as _provider_resolver
from tests.mcp.systems_support import provisioning_profile as _profile

_ARM = {"probability": 5, "times": 3}


class _Connector:
    def __init__(self, endpoint: tuple[str, int] | None) -> None:
        self._endpoint = endpoint

    def recorded_ssh_endpoint(
        self, system: object, *, ssh_forward_port: int | None = None
    ) -> tuple[str, int] | None:
        return self._endpoint


def _state(knob: str, **override: int) -> FaultKnobState:
    values = dict.fromkeys(FAULT_KNOB_PARAMETERS[knob], 0) | override
    return FaultKnobState(present=True, parameters=values)


class _Port:
    """Records calls and answers with the state the helper would print."""

    def __init__(self, *, raises: CategorizedError | None = None) -> None:
        self._raises = raises
        self.calls: list[tuple[str, object]] = []
        self.key_text: str | None = None
        self.thread: int | None = None

    def _enter(self, name: str, detail: object, key_path: str) -> None:
        self.calls.append((name, detail))
        self.key_text = Path(key_path).read_text(encoding="utf-8")
        self.key_path = key_path
        self.thread = threading.get_ident()
        if self._raises is not None:
            raise self._raises

    def read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]:
        self._enter("read", None, key_path)
        return {"failslab": _state("failslab", probability=5, times=1)}

    def set_knob(
        self, *, transport_handle: str, key_path: str, knob: str, parameters: Mapping[str, int]
    ) -> dict[str, FaultKnobState]:
        self._enter("set", (knob, dict(parameters)), key_path)
        return {knob: _state(knob, **{k: v for k, v in parameters.items() if k != "min-order"})}

    def reset(
        self, *, transport_handle: str, key_path: str, knob: str | None
    ) -> dict[str, FaultKnobState]:
        self._enter("reset", knob, key_path)
        names = [knob] if knob else list(FAULT_KNOB_PARAMETERS)
        return {name: _state(name, times=1) for name in names}


def _resolver(port: _Port | None, endpoint: tuple[str, int] | None = ("127.0.0.1", 22022)):
    base = _provider_resolver(connector=_Connector(endpoint))
    runtime = dataclasses.replace(base.resolve(ResourceKind.LOCAL_LIBVIRT), fault_injection=port)
    return ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})


async def _seed(pool, state: SystemState = SystemState.READY) -> str:
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
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_profile(),
                domain_name=f"kdive-{sid}",
            ),
        )
        await ensure_system_bootstrap_key(conn, system.id, secret_registry=SecretRegistry())
    return str(sid)


def _set(
    url, resolver, *, knob="failslab", params=None, state=SystemState.READY, role=Role.CONTRIBUTOR
):
    async def go():
        async with _pool(url) as pool:
            sid = await _seed(pool, state)
            resp = await set_fault(
                pool,
                _ctx(role=role),
                sid,
                knob,
                _ARM if params is None else params,
                resolver=resolver,
                secret_registry=SecretRegistry(),
            )
            return resp, await _audit_rows(pool)

    return asyncio.run(go())


def _reset(url, resolver, *, knob=None, state=SystemState.READY, role=Role.CONTRIBUTOR):
    async def go():
        async with _pool(url) as pool:
            sid = await _seed(pool, state)
            resp = await reset_faults(
                pool,
                _ctx(role=role),
                sid,
                knob,
                resolver=resolver,
                secret_registry=SecretRegistry(),
            )
            return resp, await _audit_rows(pool)

    return asyncio.run(go())


async def _audit_rows(pool) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a.tool, a.args_digest FROM audit_log a JOIN systems s ON s.id = a.object_id "
            "WHERE a.tool LIKE 'faultinject.%' AND a.object_kind = 'systems'"
        )
        return [(row[0], row[1]) for row in await cur.fetchall()]


def test_set_arms_a_knob_reads_it_back_audits_and_removes_the_key(migrated_url: str) -> None:
    port = _Port()
    resp, audited = _set(migrated_url, _resolver(port))
    assert resp.status == "ok"
    assert resp.data["knobs"] == {
        "failslab": {
            "present": True,
            "armed": True,
            "probability": 5,
            "interval": 0,
            "times": 3,
            "space": 0,
            "verbose": 0,
        }
    }
    assert resp.suggested_next_actions == ["faultinject.inspect", "faultinject.reset"]
    assert port.calls == [("set", ("failslab", _ARM))]
    assert port.key_text and "PRIVATE KEY" in port.key_text
    assert not os.path.exists(port.key_path)
    assert port.thread != threading.get_ident()
    assert audited == [("faultinject.set", args_digest({"knob": "failslab", **_ARM}))]


def test_set_passes_fail_page_alloc_optional_parameters(migrated_url: str) -> None:
    port = _Port()
    params = {**_ARM, "interval": 2, "space": 4096, "verbose": 1, "min-order": 3}
    resp, _ = _set(migrated_url, _resolver(port), knob="fail_page_alloc", params=params)
    assert resp.status == "ok"
    assert port.calls == [("set", ("fail_page_alloc", params))]


@pytest.mark.parametrize(
    ("knob", "params", "reason"),
    [
        ("kmalloc", _ARM, "unknown_knob"),
        ("failslab", {**_ARM, "times": -1}, "parameter_out_of_range"),
        ("failslab", {**_ARM, "times": 0}, "parameter_out_of_range"),
        ("failslab", {**_ARM, "times": 1_000_001}, "parameter_out_of_range"),
        ("failslab", {**_ARM, "probability": 0}, "parameter_out_of_range"),
        ("failslab", {**_ARM, "probability": 101}, "parameter_out_of_range"),
        ("failslab", {"probability": 5}, "parameter_out_of_range"),
        ("failslab", {**_ARM, "min-order": 1}, "parameter_not_applicable"),
    ],
)
def test_rejected_set_never_reaches_the_guest(
    migrated_url: str, knob: str, params: dict[str, int], reason: str
) -> None:
    port = _Port()
    resp, audited = _set(migrated_url, _resolver(port), knob=knob, params=params)
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.data["reason"] == reason
    assert port.calls == [] and audited == []


def test_unknown_knob_lists_the_accepted_values(migrated_url: str) -> None:
    resp, _ = _set(migrated_url, _resolver(_Port()), knob="kmalloc")
    assert resp.data["accepted_values"] == ["fail_page_alloc", "failslab"]


def test_out_of_range_names_the_bound(migrated_url: str) -> None:
    resp, _ = _set(migrated_url, _resolver(_Port()), params={**_ARM, "times": -1})
    assert (resp.data["parameter"], resp.data["minimum"], resp.data["maximum"]) == (
        "times",
        1,
        1_000_000,
    )


def test_reset_one_knob_and_all_knobs(migrated_url: str) -> None:
    port = _Port()
    one, audited = _reset(migrated_url, _resolver(port), knob="fail_page_alloc")
    every, _ = _reset(migrated_url, _resolver(port))
    assert set(one.data["knobs"]) == {"fail_page_alloc"}
    assert set(every.data["knobs"]) == {"failslab", "fail_page_alloc"}
    assert every.data["knobs"]["failslab"]["armed"] is False
    assert one.suggested_next_actions == ["faultinject.inspect"]
    assert port.calls == [("reset", "fail_page_alloc"), ("reset", None)]
    assert audited == [("faultinject.reset", args_digest({"knob": "fail_page_alloc"}))]


def test_reset_rejects_an_unknown_knob(migrated_url: str) -> None:
    port = _Port()
    resp, _ = _reset(migrated_url, _resolver(port), knob="../x")
    assert resp.data["reason"] == "unknown_knob"
    assert port.calls == []


@pytest.mark.parametrize("call", ["set", "reset"])
def test_a_viewer_is_denied(migrated_url: str, call: str) -> None:
    port = _Port()
    run = _set if call == "set" else _reset
    with pytest.raises(AuthorizationError):
        run(migrated_url, _resolver(port), role=Role.VIEWER)
    assert port.calls == []


@pytest.mark.parametrize("call", ["set", "reset"])
def test_a_system_that_is_not_ready_is_a_readiness_failure(migrated_url: str, call: str) -> None:
    port = _Port()
    run = _set if call == "set" else _reset
    resp, audited = run(migrated_url, _resolver(port), state=SystemState.PROVISIONING)
    assert resp.error_category == ErrorCategory.READINESS_FAILURE.value
    assert port.calls == [] and audited == []


@pytest.mark.parametrize("call", ["set", "reset"])
def test_provider_without_the_port_is_capability_unsupported(migrated_url: str, call: str) -> None:
    run = _set if call == "set" else _reset
    resp, _ = run(migrated_url, _resolver(None))
    assert resp.data["reason"] == "capability_unsupported"
    assert resp.data["supported"] == ["local-libvirt"]


@pytest.mark.parametrize("call", ["set", "reset"])
def test_missing_ssh_forward_is_ssh_not_provisioned(migrated_url: str, call: str) -> None:
    run = _set if call == "set" else _reset
    resp, _ = run(migrated_url, _resolver(_Port(), endpoint=None))
    assert resp.data["reason"] == "ssh_not_provisioned"


@pytest.mark.parametrize("call", ["set", "reset"])
@pytest.mark.parametrize(
    ("category", "reason"),
    [
        (ErrorCategory.CONFIGURATION_ERROR, "debugfs_not_mounted"),
        (ErrorCategory.CONFIGURATION_ERROR, "knob_absent"),
        (ErrorCategory.CONFIGURATION_ERROR, "fault_injection_unsupported"),
        (ErrorCategory.CONFIGURATION_ERROR, "helper_not_installed"),
        (ErrorCategory.TRANSPORT_FAILURE, "timeout"),
        (ErrorCategory.INFRASTRUCTURE_FAILURE, "helper_failed"),
    ],
)
def test_port_errors_keep_their_category_and_audit_only_unknown_outcomes(
    migrated_url: str, call: str, category: ErrorCategory, reason: str
) -> None:
    error = CategorizedError("remediation", category=category, details={"reason": reason})
    run = _set if call == "set" else _reset
    resp, audited = run(migrated_url, _resolver(_Port(raises=error)))
    assert resp.error_category == category.value
    assert resp.data["reason"] == reason
    unknown = category is not ErrorCategory.CONFIGURATION_ERROR
    assert [tool for tool, _ in audited] == ([f"faultinject.{call}"] if unknown else [])


def test_inspect_suggests_reset_only_while_a_knob_is_armed(migrated_url: str) -> None:
    class Idle(_Port):
        def read_state(self, *, transport_handle: str, key_path: str):
            return {"failslab": _state("failslab", probability=0, times=1)}

    async def go(port: _Port):
        async with _pool(migrated_url) as pool:
            sid = await _seed(pool)
            return await inspect_faults(
                pool,
                _ctx(role=Role.VIEWER),
                sid,
                resolver=_resolver(port),
                secret_registry=SecretRegistry(),
            )

    assert asyncio.run(go(Idle())).suggested_next_actions == ["systems.get"]
    assert asyncio.run(go(_Port())).suggested_next_actions == ["faultinject.reset", "systems.get"]


@pytest.mark.parametrize("call", ["set", "reset"])
def test_an_external_boot_denial_blocks_the_write_before_the_guest(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    seen: list[ExternalBootOperation] = []

    async def deny(conn, system_id, operation, *, project, run_id=None):
        seen.append(operation)
        raise ExternalBootDenied(
            "denied",
            details={"reason": "external_boot_restricted"},
            next_actions=[],
            project=project,
        )

    monkeypatch.setattr(faultinject_module, "check_external_boot_admission", deny)
    port = _Port()
    run = _set if call == "set" else _reset
    resp, audited = run(migrated_url, _resolver(port))
    assert resp.error_category == ErrorCategory.CONFLICT.value
    assert resp.data["reason"] == "external_boot_restricted"
    assert seen == [ExternalBootOperation.FAULT_INJECTION]
    assert port.calls == [] and audited == []


def test_inspect_is_not_subject_to_the_external_boot_matrix(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args, **_kwargs):
        raise AssertionError("inspect must not consult the matrix")

    monkeypatch.setattr(faultinject_module, "check_external_boot_admission", fail)

    async def go():
        async with _pool(migrated_url) as pool:
            sid = await _seed(pool)
            return await inspect_faults(
                pool,
                _ctx(role=Role.VIEWER),
                sid,
                resolver=_resolver(_Port()),
                secret_registry=SecretRegistry(),
            )

    assert asyncio.run(go()).status == "ok"
