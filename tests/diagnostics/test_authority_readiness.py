"""Closed worker-vantage authority readiness (ADR-0606)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from typing import cast

import pytest

from kdive.diagnostics.checks import CheckStatus, Vantage
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.authority import AuthorityRequestSender

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", [None, "absent", "missing", "tls", "inactive", "timeout"])
async def test_readiness_is_closed_and_maps_failures(failure: str | None) -> None:
    from kdive.providers.remote_libvirt.diagnostics.authority import AuthorityReadinessCheck

    class Sender:
        async def health(self, *, deadline: float):
            if failure in {"tls", "inactive"}:
                raise CategorizedError(
                    "private endpoint", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                )
            if failure == "timeout":
                raise TimeoutError("private endpoint")

    def build():
        if failure == "missing":
            raise CategorizedError("private ref", category=ErrorCategory.CONFIGURATION_ERROR)
        return None if failure == "absent" else Sender()

    check = AuthorityReadinessCheck(build)
    assert check.vantage is Vantage.WORKER
    result = await check.run()
    assert result.resource_id is None
    assert "private" not in str(asdict(result))
    assert result.data == {
        "readiness": "ready"
        if failure is None
        else "unadvertised"
        if failure == "absent"
        else "unavailable"
    }
    assert result.status is (CheckStatus.PASS if failure is None else CheckStatus.ERROR)
    assert result.failure_category is (None if failure is None else ErrorCategory.READINESS_FAILURE)


async def test_health_deadline_cancels_stalled_sender_and_preserves_external_cancellation(
    monkeypatch,
):
    from kdive.providers.remote_libvirt.diagnostics import authority

    stopped = asyncio.Event()
    started = asyncio.Event()

    async def health(*, deadline: float):
        assert deadline > asyncio.get_running_loop().time()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(authority, "_HEALTH_BUDGET_SECONDS", 0.02)
    sender = cast(AuthorityRequestSender, SimpleNamespace(health=health))
    check = authority.AuthorityReadinessCheck(lambda: sender)
    result = await check.run()
    assert result.failure_category is ErrorCategory.READINESS_FAILURE
    assert stopped.is_set()
    started.clear()
    stopped.clear()
    task = asyncio.create_task(check.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_worker_diagnostic_contribution_routes_each_resource_without_disclosing_identity(
    monkeypatch,
):
    from kdive.providers.remote_libvirt.config import (
        RemoteAuthorityBinding,
        RemoteLibvirtConfig,
        TlsCertRefs,
    )
    from kdive.providers.remote_libvirt.diagnostics import contribution

    configs = {
        name: RemoteLibvirtConfig(
            uri="qemu+tls://example.invalid/system",
            cert_refs=TlsCertRefs("cert", "key", "ca"),
            concurrent_allocation_cap=1,
            authority=RemoteAuthorityBinding(name, address, 9443, "ca", "cert", "key"),
        )
        for name, address in (("resource-a", "192.0.2.1"), ("resource-b", "192.0.2.2"))
    }
    selected = []

    def build(binding):
        selected.append(binding)
        raise OSError("private certificate ref")

    monkeypatch.setattr(contribution, "all_remote_configs_by_name", lambda: list(configs.items()))
    monkeypatch.setattr(contribution, "remote_config_for_resource", configs.__getitem__)
    checks = [
        check
        for check in contribution.diagnostic_contribution(build).worker_checks()
        if check.id == "provider_authority"
    ]
    assert len(checks) == 2
    assert selected == []
    for check in checks:
        result = await check.run()
        assert result.data == {"readiness": "unavailable"}
        assert result.resource_id is None
        assert "private" not in str(asdict(result))
    assert selected == [configs["resource-a"].authority, configs["resource-b"].authority]
