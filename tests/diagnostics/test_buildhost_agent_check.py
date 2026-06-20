"""`ephemeral_libvirt_buildhost_agent` check policy tests (ADR-0167, #544/#531).

The check aggregates per-host probe outcomes (from an injected probe) into one three-state
verdict: any AGENT_UNREACHABLE → fail (with the repair-the-base-image fix); else any
HOST_UNREACHABLE or no hosts → error (never a confident fail, never a silent pass); else pass.
The aggregate error failure_category is transport_failure only when every error cause was a
transport drop, else configuration_error. Driven by an injected probe — no libvirt, no DB.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import (
    BUILDHOST_AGENT_FIX,
    BUILDHOST_AGENT_ID,
    BuildHostAgentOutcome,
    BuildHostAgentProbe,
    BuildHostProbeResult,
    CheckStatus,
    EphemeralLibvirtBuildHostAgentCheck,
    Vantage,
)


def _probe(results: list[BuildHostProbeResult]) -> BuildHostAgentProbe:
    async def probe() -> list[BuildHostProbeResult]:
        return list(results)

    return probe


def _run(results: list[BuildHostProbeResult]):
    check = EphemeralLibvirtBuildHostAgentCheck(probe=_probe(results))
    return asyncio.run(check.run())


def test_id_and_vantage() -> None:
    check = EphemeralLibvirtBuildHostAgentCheck(probe=_probe([]))
    assert check.id == BUILDHOST_AGENT_ID == "ephemeral_libvirt_buildhost_agent"
    assert check.vantage is Vantage.SERVER


def test_all_ready_is_pass() -> None:
    result = _run([BuildHostProbeResult("a", BuildHostAgentOutcome.AGENT_READY)])
    assert result.status is CheckStatus.PASS
    assert result.fix is None
    assert result.failure_category is None


def test_any_agent_unreachable_is_fail_with_fix_and_names_host() -> None:
    result = _run(
        [
            BuildHostProbeResult("good", BuildHostAgentOutcome.AGENT_READY),
            BuildHostProbeResult("broken", BuildHostAgentOutcome.AGENT_UNREACHABLE),
        ]
    )
    assert result.status is CheckStatus.FAIL
    assert result.fix == BUILDHOST_AGENT_FIX
    assert result.failure_category == "configuration_error"
    assert "broken" in result.detail


def test_only_host_unreachable_transport_is_error_transport_failure() -> None:
    result = _run(
        [BuildHostProbeResult("x", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=True)]
    )
    assert result.status is CheckStatus.ERROR
    assert result.fix is None
    assert result.failure_category == "transport_failure"


def test_only_host_unreachable_config_is_error_configuration_error() -> None:
    result = _run(
        [BuildHostProbeResult("x", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=False)]
    )
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "configuration_error"


def test_mixed_unreachable_causes_is_configuration_error() -> None:
    result = _run(
        [
            BuildHostProbeResult("t", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=True),
            BuildHostProbeResult(
                "c", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=False
            ),
        ]
    )
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "configuration_error"


def test_no_hosts_is_error_configuration_error() -> None:
    result = _run([])
    assert result.status is CheckStatus.ERROR
    assert result.failure_category == "configuration_error"
    assert "no ephemeral_libvirt build host" in result.detail


def test_fail_dominates_a_concurrent_host_unreachable() -> None:
    result = _run(
        [
            BuildHostProbeResult(
                "down", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=True
            ),
            BuildHostProbeResult("broken", BuildHostAgentOutcome.AGENT_UNREACHABLE),
        ]
    )
    assert result.status is CheckStatus.FAIL


def test_fix_names_the_base_image_remediation() -> None:
    assert "base" in BUILDHOST_AGENT_FIX and "guest agent" in BUILDHOST_AGENT_FIX


def test_fix_cites_the_staging_doc_as_an_mcp_resource_uri() -> None:
    # The remediation reaches an MCP client, which cannot open a bare filesystem path; it must
    # cite the doc as the fetchable resource URI (ADR-0151), not "docs/operating/...".
    assert "resource://kdive/docs/operating/build-source-staging.md" in BUILDHOST_AGENT_FIX
    assert "(docs/operating/" not in BUILDHOST_AGENT_FIX
