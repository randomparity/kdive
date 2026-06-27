"""`provider_tls` and `gdbstub_acl` worker-vantage check tests (ADR-0091 §2).

Both probe a per-provider contract, so both carry the `provider` they pertain to. The
three-state mapping is asserted against seeded-broken / seeded-healthy / cannot-run
fixtures, including the *exact* fix string on `fail` — a confident wrong fix is the worst
failure a diagnostic can have, so the fix is pinned, not approximated.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    CheckStatus,
)
from kdive.diagnostics.provider_checks import (
    GdbstubAclCheck,
    GdbstubAclProbe,
    ProviderTlsCheck,
    TlsProbe,
    TlsProbeOutcome,
)

_PROVIDER = "remote-libvirt"
_CA_PATH = "/etc/kdive/ca.pem"
_GDB_HOST = "10.0.0.5"
_PORT_RANGE = "47000-47099"


# ---- provider_tls -------------------------------------------------------------------


def _tls_probe(outcome: TlsProbeOutcome, *, calls: list[str] | None = None) -> TlsProbe:
    async def _probe(ca_path: str) -> TlsProbeOutcome:
        if calls is not None:
            calls.append(ca_path)
        return outcome

    return _probe


def test_provider_tls_valid_chain_is_pass() -> None:
    calls: list[str] = []
    check = ProviderTlsCheck(
        provider=_PROVIDER,
        ca_path=_CA_PATH,
        probe=_tls_probe(TlsProbeOutcome.VALID, calls=calls),
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.provider == _PROVIDER
    assert result.check_id == PROVIDER_TLS_ID
    assert result.detail == f"provider TLS chain validates against {_CA_PATH}"
    # The probe is driven against the configured CA path, not a substitute.
    assert calls == [_CA_PATH]


def test_provider_tls_invalid_cert_is_fail_with_fix() -> None:
    check = ProviderTlsCheck(
        provider=_PROVIDER, ca_path=_CA_PATH, probe=_tls_probe(TlsProbeOutcome.INVALID)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.provider == _PROVIDER
    assert result.check_id == PROVIDER_TLS_ID
    assert result.detail == f"provider cert not signed by configured CA {_CA_PATH}"
    assert result.fix == (
        f"provider cert not signed by configured CA {_CA_PATH}; reissue or set KDIVE_PROVIDER_CA"
    )


def test_provider_tls_host_unreachable_is_error_not_fail() -> None:
    check = ProviderTlsCheck(
        provider=_PROVIDER, ca_path=_CA_PATH, probe=_tls_probe(TlsProbeOutcome.UNREACHABLE)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.fix is None
    assert result.provider == _PROVIDER
    assert result.check_id == PROVIDER_TLS_ID
    assert result.detail == "provider host unreachable; cannot validate the TLS chain"


# ---- gdbstub_acl --------------------------------------------------------------------


def _acl_probe(
    *, admitted: bool | None, calls: list[tuple[str, str]] | None = None
) -> GdbstubAclProbe:
    async def _probe(host: str, port_range: str) -> bool | None:
        if calls is not None:
            calls.append((host, port_range))
        return admitted

    return _probe


def test_gdbstub_acl_range_admitted_is_pass() -> None:
    calls: list[tuple[str, str]] = []
    check = GdbstubAclCheck(
        provider=_PROVIDER,
        host=_GDB_HOST,
        port_range=_PORT_RANGE,
        probe=_acl_probe(admitted=True, calls=calls),
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.provider == _PROVIDER
    assert result.check_id == GDBSTUB_ACL_ID
    assert result.detail == f"ACL on {_GDB_HOST} admits gdbstub range {_PORT_RANGE}"
    # The probe is driven against the configured host and range, not substitutes.
    assert calls == [(_GDB_HOST, _PORT_RANGE)]


def test_gdbstub_acl_range_blocked_is_fail_with_fix() -> None:
    check = GdbstubAclCheck(
        provider=_PROVIDER,
        host=_GDB_HOST,
        port_range=_PORT_RANGE,
        probe=_acl_probe(admitted=False),
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.provider == _PROVIDER
    assert result.check_id == GDBSTUB_ACL_ID
    assert result.detail == f"gdbstub port range {_PORT_RANGE} on {_GDB_HOST} blocked"
    assert result.fix == (
        f"gdbstub port range {_PORT_RANGE} on {_GDB_HOST} blocked; "
        "open the host firewall / ACL for it"
    )


def test_gdbstub_acl_indeterminate_is_error() -> None:
    check = GdbstubAclCheck(
        provider=_PROVIDER,
        host=_GDB_HOST,
        port_range=_PORT_RANGE,
        probe=_acl_probe(admitted=None),
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.fix is None
    assert result.provider == _PROVIDER
    assert result.check_id == GDBSTUB_ACL_ID
    assert result.detail == f"could not determine the ACL on {_GDB_HOST} for {_PORT_RANGE}"
