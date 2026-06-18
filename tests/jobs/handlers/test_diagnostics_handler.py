"""Tests for the diagnostics_worker_check job handler (ADR-0163)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.diagnostics.result_codec import deserialize_results
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.diagnostics import diagnostics_worker_check_handler
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs


class _FakeCheck(Check):
    def __init__(self, result: CheckResult) -> None:
        self._result = result

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        return self._result


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "ca"),
        concurrent_allocation_cap=1,
        gdb_addr="host.example",
    )


def test_handler_runs_checks_and_serializes_inline() -> None:
    results = [
        CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
        CheckResult(GDBSTUB_ACL_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
    ]

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None,
            job=None,
            config_factory=_config,
            build_checks=lambda _config: [_FakeCheck(r) for r in results],
        )

    raw = asyncio.run(_run())
    assert {r.check_id for r in deserialize_results(raw)} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}


def test_handler_propagates_config_error() -> None:
    def boom() -> RemoteLibvirtConfig:
        raise CategorizedError("bad inventory", category=ErrorCategory.CONFIGURATION_ERROR)

    async def _run() -> str | None:
        return await diagnostics_worker_check_handler(
            conn=None, job=None, config_factory=boom, build_checks=lambda _c: []
        )

    with pytest.raises(CategorizedError):
        asyncio.run(_run())
