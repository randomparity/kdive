"""Tests for the direct-TLS provider_tls probe (ADR-0164)."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from kdive.diagnostics.checks import TlsProbeOutcome
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.diagnostics.provider_tls import provider_tls_probe, tls_endpoint


def _config(uri: str = "qemu+tls://host.example/system") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=TlsCertRefs("c", "k", "ca"),
        concurrent_allocation_cap=1,
        gdb_addr="host.example",
    )


def test_tls_endpoint_defaults_and_overrides_port() -> None:
    assert tls_endpoint("qemu+tls://host.example/system") == ("host.example", 16514)
    assert tls_endpoint("qemu+tls://host.example:17000/system") == ("host.example", 17000)


def test_tls_endpoint_empty_host_when_uri_has_no_authority() -> None:
    assert tls_endpoint("qemu+tls:///system") == ("", 16514)


@pytest.mark.parametrize(
    ("raiser", "expected"),
    [
        (None, TlsProbeOutcome.VALID),
        (ssl.SSLCertVerificationError("bad cert"), TlsProbeOutcome.INVALID),
        (ConnectionRefusedError(), TlsProbeOutcome.UNREACHABLE),
        (TimeoutError(), TlsProbeOutcome.UNREACHABLE),
        (ssl.SSLError("protocol"), TlsProbeOutcome.UNREACHABLE),
    ],
)
def test_probe_classifies(raiser: Exception | None, expected: TlsProbeOutcome) -> None:
    captured: dict[str, object] = {}
    sentinel_ctx = ssl.create_default_context()
    config = _config()

    def fake_connector(host: str, port: int, ctx: ssl.SSLContext) -> None:
        captured["host"], captured["port"], captured["ctx"] = host, port, ctx
        if raiser is not None:
            raise raiser

    def fake_context(cfg: RemoteLibvirtConfig) -> ssl.SSLContext:
        captured["config"] = cfg
        return sentinel_ctx

    probe = provider_tls_probe(config, connector=fake_connector, context_factory=fake_context)

    async def _run() -> TlsProbeOutcome:
        return await probe("ca-label")

    assert asyncio.run(_run()) == expected
    assert captured["host"] == "host.example"
    assert captured["port"] == 16514
    assert captured["config"] is config
    assert captured["ctx"] is sentinel_ctx
