"""`remote_libvirt_base_image_staging` check + probe-adapter tests (ADR-0150, #513).

The check is server-vantage: it looks up the operator-staged base-image volume on the host's
storage pool over the same `qemu+tls://` connection reachability uses, and reports three-state.
The `fail`-vs-`error` split follows ADR-0091: an absent volume on a present pool is a contract
`fail` with the ADR-0080 staging fix; a host-down / absent-pool / unresolvable-inventory is an
`error` (a stage-the-volume fix would be a confident-wrong-fix). The libvirt boundary is mocked;
the logic is not.
"""

from __future__ import annotations

import asyncio

import libvirt

from kdive.diagnostics.checks import (
    BASE_IMAGE_STAGING_ID,
    BASE_VOLUME_NOT_STAGED_FIX,
    BaseImageStagingCheck,
    BaseImageStagingOutcome,
    CheckStatus,
    Vantage,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.diagnostics.base_image_staging import base_image_staging_probe
from tests.providers.remote_libvirt.conftest import libvirt_error

_PROVIDER = "remote-libvirt"
_POOL = "default"
_VOLUME = "fedora-kdive-remote-base-43.qcow2"


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs(
            client_cert_ref="remote/clientcert.pem",
            client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret - ref name
            ca_cert_ref="remote/cacert.pem",
        ),
        concurrent_allocation_cap=1,
        storage_pool=_POOL,
        gdb_addr="10.0.0.5",
    )


class _FakeBackend:
    def resolve(self, ref: str) -> str:
        return f"-----material for {ref}-----"


def _probe(outcome: BaseImageStagingOutcome):
    async def probe() -> BaseImageStagingOutcome:
        return outcome

    return probe


# ---- check logic --------------------------------------------------------------------


def test_staged_is_pass() -> None:
    check = BaseImageStagingCheck(provider=_PROVIDER, probe=_probe(BaseImageStagingOutcome.STAGED))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.failure_category is None
    assert result.fix is None
    assert result.provider == _PROVIDER


def test_not_staged_is_fail_configuration_error_with_staging_fix() -> None:
    check = BaseImageStagingCheck(
        provider=_PROVIDER, probe=_probe(BaseImageStagingOutcome.NOT_STAGED)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.failure_category == "configuration_error"
    assert result.fix == BASE_VOLUME_NOT_STAGED_FIX
    assert result.provider == _PROVIDER


def test_unreachable_is_error_transport_failure_no_fix() -> None:
    check = BaseImageStagingCheck(
        provider=_PROVIDER, probe=_probe(BaseImageStagingOutcome.UNREACHABLE)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "transport_failure"
    assert result.fix is None
    assert result.provider == _PROVIDER


def test_indeterminate_is_error_configuration_error_no_fix() -> None:
    check = BaseImageStagingCheck(
        provider=_PROVIDER, probe=_probe(BaseImageStagingOutcome.INDETERMINATE)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "configuration_error"
    assert result.fix is None
    assert result.provider == _PROVIDER


def test_check_id_and_vantage() -> None:
    check = BaseImageStagingCheck(provider=_PROVIDER, probe=_probe(BaseImageStagingOutcome.STAGED))
    assert check.id == BASE_IMAGE_STAGING_ID == "remote_libvirt_base_image_staging"
    assert check.vantage is Vantage.SERVER


# ---- production probe adapter (libvirt boundary mocked) ------------------------------


class _FakeVolume:
    # Satisfies the Volume protocol (storage.py); the probe never reads these, but typing the
    # fake against the real protocol keeps the connection-slice plumbing honest.
    def path(self) -> str:
        return "/pool/vol"

    def info(self) -> list[int]:
        return [0, 0, 0]

    def delete(self, flags: int = 0) -> int:
        del flags
        return 0


class _FakePool:
    def __init__(self, *, volumes: set[str], vol_error: libvirt.libvirtError | None = None) -> None:
        self._volumes = volumes
        self._vol_error = vol_error

    def storageVolLookupByName(self, name: str) -> _FakeVolume:  # noqa: N802
        if self._vol_error is not None:
            raise self._vol_error
        if name in self._volumes:
            return _FakeVolume()
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)

    def createXML(self, xml: str, flags: int = 0) -> _FakeVolume:  # noqa: N802
        del xml, flags
        return _FakeVolume()


class _FakeConn:
    def __init__(
        self,
        *,
        pools: dict[str, _FakePool] | None = None,
        pool_error: libvirt.libvirtError | None = None,
    ) -> None:
        self._pools = pools if pools is not None else {_POOL: _FakePool(volumes={_VOLUME})}
        self._pool_error = pool_error
        self.closed = False

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802
        if self._pool_error is not None:
            raise self._pool_error
        if name in self._pools:
            return self._pools[name]
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)

    def close(self) -> None:
        self.closed = True


def _run_probe(probe) -> BaseImageStagingOutcome:
    async def _drive() -> BaseImageStagingOutcome:
        return await probe()

    return asyncio.run(_drive())


def _build(open_connection, tmp_path, *, volume_factory=lambda: _VOLUME):
    return base_image_staging_probe(
        config_factory=_config,
        volume_factory=volume_factory,
        open_connection=open_connection,
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )


def test_adapter_staged_when_volume_present(tmp_path) -> None:
    conn = _FakeConn()
    assert _run_probe(_build(lambda uri: conn, tmp_path)) is BaseImageStagingOutcome.STAGED
    assert conn.closed is True


def test_adapter_not_staged_when_volume_absent(tmp_path) -> None:
    conn = _FakeConn(pools={_POOL: _FakePool(volumes=set())})
    assert _run_probe(_build(lambda uri: conn, tmp_path)) is BaseImageStagingOutcome.NOT_STAGED


def test_adapter_indeterminate_when_pool_absent(tmp_path) -> None:
    conn = _FakeConn(pools={})
    assert _run_probe(_build(lambda uri: conn, tmp_path)) is BaseImageStagingOutcome.INDETERMINATE


def test_adapter_indeterminate_on_unexpected_storage_error(tmp_path) -> None:
    # A libvirtError from the storage RPC after a successful open (transport drop / internal error)
    # is indeterminate, never a confident NOT_STAGED — host-down is the reachability check's job.
    conn = _FakeConn(
        pools={
            _POOL: _FakePool(volumes=set(), vol_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
        }
    )
    assert _run_probe(_build(lambda uri: conn, tmp_path)) is BaseImageStagingOutcome.INDETERMINATE


def test_adapter_unreachable_on_connect_error(tmp_path) -> None:
    def open_connection(uri: str) -> _FakeConn:
        raise libvirt.libvirtError("connect failed")

    assert _run_probe(_build(open_connection, tmp_path)) is BaseImageStagingOutcome.UNREACHABLE


def test_adapter_indeterminate_when_config_unresolvable(tmp_path) -> None:
    opener_called = False

    def open_connection(uri: str) -> _FakeConn:
        nonlocal opener_called
        opener_called = True
        return _FakeConn()

    def bad_config() -> RemoteLibvirtConfig:
        raise CategorizedError(
            "multiple [[remote_libvirt]] instances declared",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    probe = base_image_staging_probe(
        config_factory=bad_config,
        volume_factory=lambda: _VOLUME,
        open_connection=open_connection,
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is BaseImageStagingOutcome.INDETERMINATE
    assert opener_called is False


def test_adapter_indeterminate_when_volume_factory_unresolvable(tmp_path) -> None:
    opener_called = False

    def open_connection(uri: str) -> _FakeConn:
        nonlocal opener_called
        opener_called = True
        return _FakeConn()

    def bad_volume() -> str:
        raise CategorizedError(
            "image source is not staged",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    assert (
        _run_probe(_build(open_connection, tmp_path, volume_factory=bad_volume))
        is BaseImageStagingOutcome.INDETERMINATE
    )
    assert opener_called is False
