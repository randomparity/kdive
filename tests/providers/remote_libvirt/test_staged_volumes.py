"""Production staged-volume probe: maps libvirt pool lookups to per-volume status strings."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt import staged_volumes
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from tests.providers.remote_libvirt.conftest import libvirt_error


def _config(pool: str = "kdive-pool") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host/system",
        cert_refs=TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a"),
        concurrent_allocation_cap=1,
        storage_pool=pool,
    )


class _Vol:
    # Satisfies the storage.py Volume protocol; the probe never reads these.
    def path(self) -> str:
        return "/pool/vol"

    def info(self) -> list[int]:
        return [0, 0, 0]

    def delete(self, flags: int = 0) -> int:
        del flags
        return 0


class _Pool:
    def __init__(self, staged: set[str]) -> None:
        self._staged = staged

    def storageVolLookupByName(self, name: str) -> _Vol:  # noqa: N802
        if name in self._staged:
            return _Vol()
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> _Vol:  # noqa: N802
        del xml, flags
        return _Vol()


class _Conn:
    def __init__(self, staged: set[str], *, pool_exists: bool = True) -> None:
        self._staged = staged
        self._pool_exists = pool_exists

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        if not self._pool_exists:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)
        return _Pool(self._staged)

    def close(self) -> None:
        pass


class _FakeBackend:
    # Mocks the secret boundary so remote_connection's pkipath materialization succeeds without
    # real cert refs (mirrors tests/diagnostics/test_base_image_staging.py).
    def resolve(self, ref: str) -> str:
        return f"-----material for {ref}-----"


def _backend() -> _FakeBackend:
    return _FakeBackend()


def _probe(
    volumes,
    *,
    conn=None,
    config_exc=None,
    transport_exc=False,
    block=False,
    timeout=5.0,
    tmp_path=None,
):
    def config_factory():
        if config_exc is not None:
            raise config_exc
        return _config()

    def open_connection(uri):
        if transport_exc:
            raise libvirt.libvirtError("connect refused")
        if block:
            # Exceed the injected timeout so wait_for fires first, but stay small: a to_thread
            # worker is not cancellable, so keep the orphaned sleep short to not stall teardown.
            time.sleep(1.0)
        return conn

    return asyncio.run(
        staged_volumes.probe_staged_volumes(
            volumes,
            config_factory=config_factory,
            open_connection=open_connection,
            secret_backend_factory=_backend,
            timeout=timeout,
            pki_base_dir=tmp_path,
        )
    )


def test_maps_staged_absent(tmp_path: Path) -> None:
    conn = _Conn(staged={"a.qcow2"})
    out = _probe(["a.qcow2", "b.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "staged", "b.qcow2": "absent"}


def test_pool_absent(tmp_path: Path) -> None:
    conn = _Conn(staged=set(), pool_exists=False)
    out = _probe(["a.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "pool_absent"}


def test_transport_failure_is_unreachable(tmp_path: Path) -> None:
    out = _probe(["a.qcow2"], transport_exc=True, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unreachable"}


def test_config_error_is_unknown_and_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    exc = CategorizedError("no instance", category=ErrorCategory.CONFIGURATION_ERROR)
    with caplog.at_level(logging.WARNING, logger=staged_volumes.__name__):
        out = _probe(["a.qcow2"], config_exc=exc, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unknown"}
    assert any(
        r.message == "staged-volume probe could not resolve remote config" for r in caplog.records
    )


def test_timeout_is_unreachable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # A blocking connect with a tiny injected timeout must degrade to unreachable, fast.
    with caplog.at_level(logging.WARNING, logger=staged_volumes.__name__):
        out = _probe(["a.qcow2", "b.qcow2"], block=True, timeout=0.05, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unreachable", "b.qcow2": "unreachable"}
    assert any(r.message == "staged-volume probe timed out after 0.05s" for r in caplog.records)


class _PostOpenLibvirtErrorConn:
    """A conn whose storage lookup raises a libvirtError the staged map does not handle."""

    def __init__(self, code: int) -> None:
        self._code = code

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        del name
        # _PostOpenLibvirtErrorPool duck-types the pool seam (only the lookup path is exercised).
        return cast("_Pool", _PostOpenLibvirtErrorPool(self._code))

    def close(self) -> None:
        pass


class _PostOpenLibvirtErrorPool:
    def __init__(self, code: int) -> None:
        self._code = code

    def storageVolLookupByName(self, name: str) -> _Vol:  # noqa: N802
        del name
        raise libvirt_error(self._code)


def test_post_open_libvirt_error_is_unreachable_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A post-open libvirtError that is NOT NO_STORAGE_POOL/NO_STORAGE_VOL is re-raised by
    # lookup_volume_staged; the probe degrades the verdict to "unreachable" and warns.
    conn = _PostOpenLibvirtErrorConn(libvirt.VIR_ERR_INTERNAL_ERROR)
    with caplog.at_level(logging.WARNING, logger=staged_volumes.__name__):
        out = _probe(["a.qcow2", "b.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unreachable", "b.qcow2": "unreachable"}
    assert any(r.message == "staged-volume probe storage lookup failed" for r in caplog.records)


class _PostOpenCategorizedErrorConn:
    """A conn whose storage lookup raises a non-transport CategorizedError after open."""

    def __init__(self, category: ErrorCategory) -> None:
        self._category = category

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        del name
        raise CategorizedError("post-open failure", category=self._category)

    def close(self) -> None:
        pass


def test_post_open_non_transport_categorized_error_is_unknown(tmp_path: Path) -> None:
    # A CategorizedError surfacing inside the open connection with a category other than
    # TRANSPORT_FAILURE must take the verdict-"unknown" fallthrough, not "unreachable".
    conn = _PostOpenCategorizedErrorConn(ErrorCategory.CONFIGURATION_ERROR)
    out = _probe(["a.qcow2", "b.qcow2"], conn=conn, tmp_path=tmp_path)
    assert out == {"a.qcow2": "unknown", "b.qcow2": "unknown"}


class _PoolNameRecordingConn:
    """A conn that records which storage pool name the probe looks up."""

    def __init__(self, staged: set[str]) -> None:
        self._staged = staged
        self.looked_up: list[str] = []

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        self.looked_up.append(name)
        return _Pool(self._staged)

    def close(self) -> None:
        pass


def test_lookup_uses_the_configured_storage_pool(tmp_path: Path) -> None:
    conn = _PoolNameRecordingConn(staged={"a.qcow2"})

    def config_factory() -> RemoteLibvirtConfig:
        return _config(pool="distinct-pool")

    def open_connection(uri: str) -> _PoolNameRecordingConn:
        del uri
        return conn

    out = asyncio.run(
        staged_volumes.probe_staged_volumes(
            ["a.qcow2"],
            config_factory=config_factory,
            open_connection=open_connection,
            secret_backend_factory=_backend,
            pki_base_dir=tmp_path,
        )
    )

    assert out == {"a.qcow2": "staged"}
    assert conn.looked_up == ["distinct-pool"]


def test_empty_volumes_opens_nothing() -> None:
    def config_factory():
        raise AssertionError("config must not be resolved for an empty volume list")

    def open_connection(uri: str) -> _Conn:
        raise AssertionError("connection must not be opened for an empty volume list")

    out = asyncio.run(
        staged_volumes.probe_staged_volumes(
            [],
            config_factory=config_factory,
            open_connection=open_connection,
            secret_backend_factory=_backend,
        )
    )
    assert out == {}
