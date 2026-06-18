"""`remote_libvirt_reachability` check + probe-adapter tests (ADR-0125, #453).

The check is server-vantage: it opens the `qemu+tls://` libvirt client connection the server
itself makes and reports three-state. The `fail`-vs-`error` split is driven by the
``CategorizedError.category`` the connection raises — ``transport_failure`` (unreachable host) is a
contract ``fail``; ``configuration_error`` (bad URI/cert/inventory) is a check-cannot-run ``error``,
never a confident "host down". The boundary (the libvirt connection) is mocked; the logic is not.
"""

from __future__ import annotations

import asyncio

import libvirt

from kdive.diagnostics.checks import (
    REACHABILITY_ID,
    CheckStatus,
    ReachabilityOutcome,
    ReachabilityProbe,
    RemoteLibvirtReachabilityCheck,
    Vantage,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.diagnostics.reachability import (
    remote_libvirt_reachability_probe,
)

_PROVIDER = "remote-libvirt"


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs(
            client_cert_ref="remote/clientcert.pem",
            client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret - ref name
            ca_cert_ref="remote/cacert.pem",
        ),
        concurrent_allocation_cap=1,
        gdb_addr="10.0.0.5",
    )


class _FakeBackend:
    """A SecretBackend double: resolve returns the ref name (no real secret file)."""

    def resolve(self, ref: str) -> str:
        return f"-----material for {ref}-----"


class _FakeConn:
    def __init__(self, *, info: list[int] | None = None) -> None:
        self._info = info if info is not None else [0]
        self.closed = False

    def getInfo(self) -> list[int]:  # noqa: N802 - libvirt binding name
        return self._info

    def close(self) -> None:
        self.closed = True


def _probe(outcome: ReachabilityOutcome) -> ReachabilityProbe:
    async def probe() -> ReachabilityOutcome:
        return outcome

    return probe


# ---- check logic --------------------------------------------------------------------


def test_reachable_is_pass() -> None:
    check = RemoteLibvirtReachabilityCheck(
        provider=_PROVIDER, probe=_probe(ReachabilityOutcome.REACHABLE)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.failure_category is None
    assert result.fix is None
    assert result.provider == _PROVIDER


def test_unreachable_is_fail_transport_failure_with_fix() -> None:
    check = RemoteLibvirtReachabilityCheck(
        provider=_PROVIDER, probe=_probe(ReachabilityOutcome.UNREACHABLE)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.failure_category == "transport_failure"
    assert result.fix is not None
    assert result.provider == _PROVIDER


def test_misconfigured_is_error_configuration_error_no_fix() -> None:
    check = RemoteLibvirtReachabilityCheck(
        provider=_PROVIDER, probe=_probe(ReachabilityOutcome.MISCONFIGURED)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "configuration_error"
    assert result.fix is None
    assert result.provider == _PROVIDER


def test_check_id_and_vantage() -> None:
    check = RemoteLibvirtReachabilityCheck(
        provider=_PROVIDER, probe=_probe(ReachabilityOutcome.REACHABLE)
    )
    assert check.id == REACHABILITY_ID == "remote_libvirt_reachability"
    assert check.vantage is Vantage.SERVER


# ---- production probe adapter (libvirt boundary mocked) ------------------------------


def _run_probe(probe: ReachabilityProbe) -> ReachabilityOutcome:
    async def _drive() -> ReachabilityOutcome:
        return await probe()

    return asyncio.run(_drive())


def test_adapter_reachable_when_getinfo_returns(tmp_path) -> None:
    conn = _FakeConn(info=[0, 1, 2])

    def open_connection(uri: str) -> _FakeConn:
        return conn

    probe = remote_libvirt_reachability_probe(
        config_factory=_config,
        open_connection=open_connection,
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.REACHABLE
    assert conn.closed is True


def test_adapter_unreachable_on_libvirt_connect_error(tmp_path) -> None:
    def open_connection(uri: str) -> _FakeConn:
        raise libvirt.libvirtError("connect failed")

    probe = remote_libvirt_reachability_probe(
        config_factory=_config,
        open_connection=open_connection,
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.UNREACHABLE


def test_adapter_misconfigured_when_config_factory_raises_configuration_error(tmp_path) -> None:
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

    probe = remote_libvirt_reachability_probe(
        config_factory=bad_config,
        open_connection=open_connection,
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.MISCONFIGURED
    assert opener_called is False


def test_adapter_misconfigured_when_secret_resolution_fails(tmp_path) -> None:
    class _BadBackend:
        def resolve(self, ref: str) -> str:
            from kdive.security.secrets.paths import PathSafetyError

            raise PathSafetyError(f"{ref} escapes the secrets root")

    probe = remote_libvirt_reachability_probe(
        config_factory=_config,
        open_connection=lambda uri: _FakeConn(),
        secret_backend_factory=_BadBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.MISCONFIGURED


def test_adapter_unreachable_when_getinfo_fails_after_open(tmp_path) -> None:
    class _DecliningConn(_FakeConn):
        def getInfo(self) -> list[int]:  # noqa: N802 - libvirt binding name
            raise libvirt.libvirtError("RPC declined after open")

    # remote_connection wraps a failed *open* into TRANSPORT_FAILURE, but a libvirtError from
    # getInfo() after a successful open escapes raw — the adapter maps it to UNREACHABLE rather
    # than letting it fall through to the generic backstop as an uncategorized error.
    probe = remote_libvirt_reachability_probe(
        config_factory=_config,
        open_connection=lambda uri: _DecliningConn(),
        secret_backend_factory=_FakeBackend,
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.UNREACHABLE


def test_adapter_uses_default_env_secret_backend(tmp_path, monkeypatch) -> None:
    # Exercise the PRODUCTION default secret_backend_factory (fresh registry +
    # KDIVE_SECRETS_ROOT-rooted file backend), faking only the libvirt opener so the
    # secret-resolution -> materialized_pkipath -> connect seam runs end-to-end.
    import kdive.config as config

    secrets_root = tmp_path / "secrets"
    (secrets_root / "remote").mkdir(parents=True)
    for name in ("clientcert.pem", "clientkey.pem", "cacert.pem"):
        (secrets_root / "remote" / name).write_text(f"-----material for {name}-----")
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(secrets_root))
    config.load()

    probe = remote_libvirt_reachability_probe(
        config_factory=_config,
        open_connection=lambda uri: _FakeConn(info=[0, 1, 2]),
        pki_base_dir=tmp_path,
    )
    assert _run_probe(probe) is ReachabilityOutcome.REACHABLE
