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
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.smoke.agent_smoke.surface import AppSurface
from tests.smoke.agent_smoke.walker import walk

pytestmark = pytest.mark.agent_smoke


def _built_app():
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    keypair = make_keypair()
    verifier = JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


def test_golden_path_walks_the_served_surface_without_stalling() -> None:
    result = asyncio.run(walk(AppSurface(_built_app())))

    assert result.ok, "agent-smoke stalls:\n" + "\n".join(
        f"  {stall.stage}: {stall.reason}" for stall in result.stalls
    )
    # The walk actually reached the terminal stages (not an early orient-only return).
    assert {"orient", "wind-down", "gateway", "links", "prompts"} <= set(result.visited)
