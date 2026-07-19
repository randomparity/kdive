"""Tests for the ``kdive stage-volume`` env-backed wiring (ADR-0336).

``stage_volume_wiring`` is the impure half of ``stage-volume`` (a sync DB connection, the
object store, and the remote-libvirt config resolver); ``test_stage_volume.py`` covers the
pure orchestration via a fake ``StageVolumeDeps``. These tests exercise the wiring functions
directly against psycopg/config/object-store doubles so the module's error branches run
without a live database.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from uuid import uuid4

import psycopg
import pytest

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs import stage_volume_wiring as wiring
from kdive.images.rootfs.stage_volume import _TargetRow
from kdive.images.rootfs.stage_volume_wiring import (
    _attach_config,
    _find_staged_row,
    _resolve_single_remote_config,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs


def _remote_config(uri: str = "qemu+tls://host/system") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=TlsCertRefs(client_cert_ref="cert", client_key_ref="key", ca_cert_ref="ca"),
        concurrent_allocation_cap=4,
    )


# --- _resolve_single_remote_config --------------------------------------------


def test_resolve_single_remote_config_wrong_provider_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _resolve_single_remote_config("local-libvirt")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details == {"provider": "local-libvirt"}


def test_resolve_single_remote_config_zero_instances_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiring, "all_remote_configs_by_name", lambda: [])
    with pytest.raises(CategorizedError) as excinfo:
        _resolve_single_remote_config("remote-libvirt")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details == {"instances": ""}


def test_resolve_single_remote_config_many_instances_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wiring,
        "all_remote_configs_by_name",
        lambda: [("host-b", _remote_config()), ("host-a", _remote_config())],
    )
    with pytest.raises(CategorizedError) as excinfo:
        _resolve_single_remote_config("remote-libvirt")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    # sorted, so an operator sees a stable, alphabetized fix-hint regardless of declaration order
    assert excinfo.value.details == {"instances": "host-a, host-b"}


def test_resolve_single_remote_config_returns_the_lone_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _remote_config("qemu+tls://only/system")
    monkeypatch.setattr(wiring, "all_remote_configs_by_name", lambda: [("only", cfg)])
    assert _resolve_single_remote_config("remote-libvirt") is cfg


# --- _find_staged_row: psycopg double ------------------------------------------


class _FakeCursor:
    """A psycopg-cursor double: records ``execute`` calls, scripts ``fetchone``/an error."""

    def __init__(
        self, *, row: tuple[object, ...] | None = None, raises: Exception | None = None
    ) -> None:
        self._row = row
        self._raises = raises
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append((query, params))
        if self._raises is not None:
            raise self._raises

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeConn:
    """A psycopg-connection double: ``with conn.cursor() as cur`` plus ``conn.commit()``."""

    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    @contextmanager
    def cursor(self) -> Iterator[_FakeCursor]:
        yield self._cursor

    def commit(self) -> None:
        self.committed = True


def _stub_connect(conn: _FakeConn) -> Callable[[str], AbstractContextManager[_FakeConn]]:
    @contextmanager
    def _connect(_conninfo: str) -> Iterator[_FakeConn]:
        yield conn

    return _connect


def _patch_db(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(wiring.psycopg, "connect", _stub_connect(conn))
    monkeypatch.setattr(wiring.config, "require", lambda _setting: "postgresql://db")


def test_find_staged_row_absent_row_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_db(monkeypatch, _FakeConn(_FakeCursor(row=None)))
    with pytest.raises(CategorizedError) as excinfo:
        _find_staged_row("remote-libvirt", "fedora-44", "x86_64")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details == {
        "provider": "remote-libvirt",
        "name": "fedora-44",
        "arch": "x86_64",
    }


def test_find_staged_row_present_row_returns_target_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row_id = uuid4()
    _patch_db(monkeypatch, _FakeConn(_FakeCursor(row=(row_id, "fedora-44.qcow2"))))
    assert _find_staged_row("remote-libvirt", "fedora-44", "x86_64") == _TargetRow(
        row_id=row_id, volume="fedora-44.qcow2"
    )


# --- _attach_config: object-store + psycopg doubles -----------------------------


class _FakeObjectStore:
    """Records puts; ``_attach_config`` discards ``put_artifact``'s return value."""

    def __init__(self) -> None:
        self.put_calls: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.put_calls.append(request)
        return StoredArtifact(request.key(), "etag-1", request.sensitivity, request.retention_class)


def test_attach_config_db_error_maps_to_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeObjectStore()
    monkeypatch.setattr(wiring, "object_store_from_env", lambda: store)
    _patch_db(monkeypatch, _FakeConn(_FakeCursor(raises=psycopg.OperationalError("db offline"))))

    row_id = uuid4()
    with pytest.raises(CategorizedError) as excinfo:
        _attach_config("remote-libvirt", "fedora-44", "x86_64", row_id, b"CONFIG_X=y\n")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details == {"image": "remote-libvirt/fedora-44/x86_64"}
    # the config already landed in the object store before the DB blip (advisory, not rolled back)
    assert len(store.put_calls) == 1


def test_attach_config_success_puts_then_commits_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeObjectStore()
    monkeypatch.setattr(wiring, "object_store_from_env", lambda: store)
    cursor = _FakeCursor()
    conn = _FakeConn(cursor)
    _patch_db(monkeypatch, conn)

    row_id = uuid4()
    _attach_config("remote-libvirt", "fedora-44", "x86_64", row_id, b"CONFIG_X=y\n")

    assert len(store.put_calls) == 1
    key = store.put_calls[0].key()
    assert cursor.executed == [
        ("UPDATE image_catalog SET kernel_config_key = %s WHERE id = %s", (key, row_id))
    ]
    assert conn.committed is True
