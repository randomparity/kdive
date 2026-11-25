"""Provider-owned local console reads and caller-specific snapshot failures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.control import diagnostic_sysrq, watch_for_crash
from kdive.jobs.handlers.runs import boot_evidence
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.local_libvirt import console
from kdive.providers.ports.console import ConsoleSnapshot, ConsoleSnapshotReadError
from kdive.providers.shared import console_evidence
from kdive.providers.shared.runtime_paths import libvirt_system_handle
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

_CONN = cast(AsyncConnection, object())


@pytest.fixture
def log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "console.log"
    monkeypatch.setattr(console, "console_log_path", lambda _sid: path)
    monkeypatch.setattr(console_evidence, "console_log_path", lambda _sid: path)
    return path


@pytest.mark.parametrize("body", [b"", b"\xff value=raw-console-secret\n"])
def test_local_reader_preserves_raw_capture_offsets(log_path: Path, body: bytes) -> None:
    log_path.write_bytes(body)
    reader = console.LocalLibvirtConsoleReader()
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert result.data == body
    assert result.next_index == 0
    assert result.pumped is True


def test_local_reader_missing_file_is_empty(log_path: Path) -> None:
    assert not log_path.exists()
    result = asyncio.run(console.LocalLibvirtConsoleReader().read_window(_CONN, uuid4()))
    assert result.data == b""
    assert result.next_index == 0
    assert result.pumped is True


@pytest.mark.parametrize("index", [-1, 1])
def test_local_reader_rejects_nonzero_part_window(index: int) -> None:
    with pytest.raises(ValueError, match="use start_index=0"):
        asyncio.run(console.LocalLibvirtConsoleReader().read_window(_CONN, uuid4(), index))


def test_local_reader_preserves_source_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    error = CategorizedError("source failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    def fail(_path: Path) -> bytes:
        raise error

    monkeypatch.setattr(console, "read_console_log", fail)
    with pytest.raises(CategorizedError) as caught:
        asyncio.run(console.LocalLibvirtConsoleReader().read_window(_CONN, uuid4()))
    assert caught.value is error


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "category", [ErrorCategory.CONFIGURATION_ERROR, ErrorCategory.INFRASTRUCTURE_FAILURE]
)
def test_fatal_snapshot_read_does_not_depend_on_diagnostic_string(
    monkeypatch: pytest.MonkeyPatch, category: ErrorCategory, terminal: bool
) -> None:
    error = CategorizedError(
        "source unavailable",
        category=category,
        details={"remediation": "repair source"},
        terminal=terminal,
    )

    def fail(_path: Path) -> bytes:
        raise error

    monkeypatch.setattr(console_evidence, "read_console_log", fail)
    snapshotter = console.LocalLibvirtConsoleSnapshotter(secret_registry=SecretRegistry())
    with pytest.raises(ConsoleSnapshotReadError) as caught:
        asyncio.run(
            boot_evidence.capture_run_console(
                _CONN,
                uuid4(),
                uuid4(),
                snapshotter=snapshotter,
                mark=0,
            )
        )
    assert caught.value.category is category
    assert caught.value.details == error.details
    assert caught.value.terminal is terminal
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("available", [False, True])
def test_empty_or_missing_local_snapshot_registers_nothing(log_path: Path, available: bool) -> None:
    if available:
        log_path.write_bytes(b"")
    snapshotter = console.LocalLibvirtConsoleSnapshotter(secret_registry=SecretRegistry())
    assert asyncio.run(snapshotter.mark_boot_window(uuid4())) == 0
    assert asyncio.run(snapshotter.snapshot(_CONN, uuid4(), uuid4())) is None


def test_local_snapshot_redacts_before_object_storage(
    log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SecretRegistry()
    registry.register("console-fixture-value", scope=None)
    log_path.write_bytes(b"\xff console-fixture-value booted")
    requests: list[ArtifactWriteRequest] = []

    class Store:
        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            requests.append(request)
            return StoredArtifact(
                request.key(), "etag", Sensitivity.REDACTED, "console", version_id="v1"
            )

    async def upsert(conn, sid, rid, stored, data):
        assert requests[0].data == data
        return ConsoleSnapshot(uuid4(), stored.key, data)

    monkeypatch.setattr(console, "_upsert_console_artifact_row", upsert)
    sid, rid = uuid4(), uuid4()
    snapshotter = console.LocalLibvirtConsoleSnapshotter(
        cast(ObjectStore, Store()),
        secret_registry=registry,
    )
    result = asyncio.run(snapshotter.snapshot(_CONN, sid, rid))
    assert result is not None
    assert result.data == "\ufffd [REDACTED] booted".encode()
    assert requests[0].owner_id == str(sid)
    assert requests[0].name == f"console-{rid}"
    assert requests[0].sensitivity is Sensitivity.REDACTED


@pytest.mark.parametrize("phase", ["store", "database"])
def test_registration_failure_is_best_effort_even_with_source_diagnostic(
    log_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    log_path.write_bytes(b"booted")
    error = CategorizedError(
        "registration failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"operation": "read_console_log"},
    )

    class Store:
        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            if phase == "store":
                raise error
            return StoredArtifact(
                request.key(), "etag", Sensitivity.REDACTED, "console", version_id="v1"
            )

    async def fail_database(*_args):
        raise error

    monkeypatch.setattr(console, "_upsert_console_artifact_row", fail_database)
    snapshotter = console.LocalLibvirtConsoleSnapshotter(
        cast(ObjectStore, Store()),
        secret_registry=SecretRegistry(),
    )
    assert (
        asyncio.run(
            boot_evidence.capture_run_console(
                _CONN,
                uuid4(),
                uuid4(),
                snapshotter=snapshotter,
                mark=0,
            )
        )
        is None
    )


def test_controls_reject_absent_reader_without_local_fallback() -> None:
    port = cast(Any, object())
    runtime = ProviderRuntime(
        system_handle=libvirt_system_handle,
        profile_policy=port,
        provisioner=port,
        installer=port,
        booter=port,
        connector=port,
        controller=port,
        retriever=port,
        crash_postmortem=port,
        vmcore_introspector=port,
        live_introspector=port,
    )
    binding = ProviderBinding(ResourceKind.LOCAL_LIBVIRT, runtime)
    with pytest.raises(CategorizedError) as sysrq:
        diagnostic_sysrq._console_reader(_CONN, uuid4(), None)
    with pytest.raises(CategorizedError) as watch:
        watch_for_crash._console_reader(_CONN, uuid4(), binding)
    assert sysrq.value.details["reason"] == "console_reader_unavailable"
    assert watch.value.details["reason"] == "console_reader_unavailable"
