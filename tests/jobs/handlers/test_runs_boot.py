"""Coverage anchor for the split boot run handler module, plus the runs registrar facade."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers import runs, runs_boot
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry


def test_boot_handler_facade_and_leaf_console_patch_surface() -> None:
    assert runs.boot_handler is runs_boot.boot_handler
    assert runs_boot.console_log_path is not None
    assert runs_boot.read_console_log is not None


def _ports() -> runs.RunHandlerPorts:
    return runs.RunHandlerPorts(
        resolver=cast(ProviderResolver, object()),
        secret_registry=cast(SecretRegistry, object()),
        transport_factories=cast(object, "transport-factories"),  # type: ignore[arg-type]
        artifact_store=cast(object, "artifact-store"),  # type: ignore[arg-type]
    )


def test_register_handlers_binds_each_run_kind_to_its_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The facade must bind exactly build/install/boot, each to its own leaf handler — a
    # mis-wired lambda (wrong kind, wrong handler) would let the worker dispatch a job to the
    # wrong run phase.
    calls: dict[str, tuple[object, object, dict[str, object]]] = {}

    async def _fake(label: str, conn: object, job: object, **kwargs: object) -> str:
        calls[label] = (conn, job, kwargs)
        return label

    monkeypatch.setattr(
        runs, "build_handler", lambda conn, job, **kw: _fake("build", conn, job, **kw)
    )
    monkeypatch.setattr(
        runs, "install_handler", lambda conn, job, **kw: _fake("install", conn, job, **kw)
    )
    monkeypatch.setattr(
        runs, "boot_handler", lambda conn, job, **kw: _fake("boot", conn, job, **kw)
    )

    registry = HandlerRegistry()
    ports = _ports()
    runs.register_handlers(registry, ports=ports)

    for kind in (JobKind.BUILD, JobKind.INSTALL, JobKind.BOOT):
        assert registry.get(kind) is not None
    # Run kinds the facade must NOT claim.
    assert registry.get(JobKind.PROVISION) is None

    conn, job = object(), object()
    assert asyncio.run(registry.get(JobKind.BUILD)(conn, job)) == "build"  # type: ignore[misc]
    assert asyncio.run(registry.get(JobKind.INSTALL)(conn, job)) == "install"  # type: ignore[misc]
    assert asyncio.run(registry.get(JobKind.BOOT)(conn, job)) == "boot"  # type: ignore[misc]

    # Each lambda threads the shared conn/job plus the ports the leaf handler needs.
    assert calls["build"][0] is conn and calls["build"][1] is job
    assert calls["build"][2] == {
        "resolver": ports.resolver,
        "secret_registry": ports.secret_registry,
        "transport_factories": ports.transport_factories,
        "build_phase_recorder": ports.build_phase_recorder,
    }
    assert calls["install"][0] is conn and calls["install"][1] is job
    assert calls["install"][2] == {"resolver": ports.resolver}
    assert calls["boot"][0] is conn and calls["boot"][1] is job
    assert calls["boot"][2] == {
        "resolver": ports.resolver,
        "secret_registry": ports.secret_registry,
        "artifact_store": ports.artifact_store,
    }
