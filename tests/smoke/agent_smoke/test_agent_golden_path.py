"""The gated agent-smoke pass: walk the real served surface, assert no stall (#1370, ADR-0411).

This is the ``agent_smoke`` tier — a non-PR-gate smoke that parallels ``live_vm`` /
``live_stack`` and is the harness the deferred nightly live-LLM agent will drive. Here the
deterministic walker stands in for that agent: it builds the app and walks the golden path
over the truly served surface, and the one green pass this issue asks for is this test
passing (``just test-agent-smoke``).

Infra-free: the built app runs over a closed pool plus the dummy ``KDIVE_S3_*`` test env; no
DB, S3, VM, or network. It is kept out of the default suite and the PR gate on purpose (see
ADR-0411) so the future live-LLM agent can replace this walker within the same tier without
turning a credential-dependent smoke into a required PR check.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.dev_harness import AUDIENCE, ISSUER, make_keypair
from kdive.mcp.exposure import CORE_TOOLS, gateway_enabled, visible_tool_names
from kdive.security.authz.context import context_from_claims
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.smoke.agent_smoke.surface import AppSurface, agent_claims
from tests.smoke.agent_smoke.walker import GATEWAY_TOOLS, walk

pytestmark = pytest.mark.agent_smoke


def _built_app():
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


def _expected_agent_catalog() -> set[str]:
    """The names an agent-profile connection holding the walk's grants sees from ``tools/list``.

    Derived from the server's own classification rather than pinned to a literal list: the
    gateway clips a non-``kdivectl`` caller to ``rbac_visible & CORE_TOOLS``
    (``mcp/middleware/exposure.py``), so a change to either ``CORE_TOOLS`` membership or a core
    tool's required scope moves this expectation with it (same derivation as the wire harness's
    live gateway assertions, PR #2491).
    """
    return visible_tool_names(context_from_claims(agent_claims()), CORE_TOOLS)


@pytest.mark.skipif(
    not gateway_enabled(),
    reason="KDIVE_MCP_TOOL_GATEWAY is off; the server serves the flat ADR-0148 RBAC catalog",
)
def test_walked_surface_is_the_clipped_agent_catalog() -> None:
    """The tier walks the catalog an agent receives, not the unfiltered registry (#2523).

    The golden-path walk below passes against *either* catalog, so without this assertion a
    regression to the pre-``62fb4fa4d`` flat surface — the defect #2521's survey recorded as
    entry A1 — is invisible: the tier would keep reporting green while exercising a surface no
    agent is served.

    Skipped rather than adapted when the gateway is switched off, because then the flat catalog
    is what the server is configured to serve: asserting the clip there would report a
    supported configuration as this defect, and the 100-line set diff reads identically.
    """
    names = asyncio.run(AppSurface(_built_app()).tool_names())

    assert set(names) == _expected_agent_catalog()
    assert set(GATEWAY_TOOLS) <= names  # the clip keeps the always-available gateway


def test_golden_path_walks_the_served_surface_without_stalling() -> None:
    result = asyncio.run(walk(AppSurface(_built_app())))

    assert result.ok, "agent-smoke stalls:\n" + "\n".join(
        f"  {stall.stage}: {stall.reason}" for stall in result.stalls
    )
    # The walk actually reached the terminal stages (not an early orient-only return).
    assert {"orient", "wind-down", "gateway", "links", "prompts"} <= set(result.visited)
