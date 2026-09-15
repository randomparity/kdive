"""Three-tier wire smoke test (ADR-0044): in-memory / oidc_issuer / live_stack.

The in-memory tier (Docker-free) covers the claim shape and the agent-gateway catalog
expectation; the ``oidc_issuer`` tier is the standing claim-shape gate (real issuer + real
verifier); the ``live_stack`` tier drives both exposure profiles over real HTTP — the only
tier where authenticated tool dispatch works (the in-memory transport carries no token).

The two ``live_stack`` tests assert the accepted gateway contract (ADR-0268 §4, ADR-0456),
not the pre-gateway flat catalog: a connection whose verified ``azp`` is not the configured
``kdivectl`` client id is clipped to ``CORE_TOOLS``, and only the operator CLI's own client
id sees the full RBAC-visible catalog. The proof record for that contract is
``docs/design/2026-07-27-mcp-exposure-profiles-proof-record-1582.md``.
"""

from __future__ import annotations

import asyncio

import jwt  # PyJWT: decode a token's claims without verifying the signature
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.mcp.dev_harness import (
    AUDIENCE,
    LiveStackClient,
    LiveStackToolError,
    _build_claims,
    _tool_error_text,
    make_keypair,
    mint,
    mint_token,
)
from kdive.mcp.exposure import CORE_TOOLS, visible_tool_names
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import context_from_claims
from kdive.security.authz.rbac import Role, roles_from_claims
from tests.integration.live_stack.conftest import require_issuer, require_stack

_PROJECT = "proj-a"
# (subject, roles map, platform_roles) per role token the smoke exercises.
_ROLE_SUBJECTS = (
    ("viewer-proj-a", {_PROJECT: "viewer"}, None),
    ("contributor-proj-a", {_PROJECT: "contributor"}, None),
    ("operator-proj-a", {_PROJECT: "operator"}, None),
    ("admin-proj-a", {_PROJECT: "admin"}, None),
    ("auditor", {_PROJECT: "viewer"}, ["platform_auditor"]),
)
# A tool deliberately outside CORE_TOOLS: absent from an agent's catalog, still reachable.
_UNADVERTISED_TOOL = "resources.list"


def _expected_agent_catalog(roles: dict[str, str], platform_roles: list[str] | None) -> set[str]:
    """The names an agent-profile connection holding these grants sees from ``tools/list``.

    Derived from the server's own classification map rather than pinned to a literal list:
    the gateway clips a non-``kdivectl`` caller to ``rbac_visible & CORE_TOOLS``
    (``mcp/middleware/exposure.py``), so a change to either ``CORE_TOOLS`` membership or a
    core tool's required scope moves this expectation with it instead of silently
    invalidating the live assertion.
    """
    claims = _build_claims(
        subject="catalog-probe",
        audience=AUDIENCE,
        projects=[_PROJECT],
        roles=roles,
        platform_roles=platform_roles,
        agent_session="sess-1",
    )
    return visible_tool_names(context_from_claims(claims), CORE_TOOLS)


def test_inmemory_tier_claim_shapes_round_trip() -> None:
    """_build_claims + the in-process mint produce the nested roles + platform_roles shapes."""
    keypair = make_keypair()
    token = mint(keypair, subject="auditor", projects=[_PROJECT], roles={_PROJECT: "viewer"})
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["roles"] == {_PROJECT: "viewer"}  # nested object survives the JWT

    claims = _build_claims(
        subject="auditor",
        audience=AUDIENCE,
        projects=[_PROJECT],
        roles={_PROJECT: "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert claims["roles"] == {_PROJECT: "viewer"}  # nested object


def test_inmemory_tier_agent_catalog_is_core_tools_clipped_by_rbac() -> None:
    """The catalog the live tier asserts over HTTP, derived here without a transport.

    Both sides are spelled out rather than compared back to ``CORE_TOOLS``: the live
    assertion follows drift on purpose, so something has to notice the drift. The
    contributor's count is what the ADR-0456 proof record measured over the wire (nine,
    exactly ``CORE_TOOLS`` —
    ``docs/design/2026-07-27-mcp-exposure-profiles-proof-record-1582.md`` §1) and its §2
    records the names; the record mints no viewer token, so the viewer's six are that same
    set clipped by RBAC, derived here. A change to ``CORE_TOOLS`` membership or to a core
    tool's required scope therefore reddens ``just ci`` and sends the change back to
    ADR-0456 §2, instead of silently moving what the live tier proves.
    """
    viewer = _expected_agent_catalog({_PROJECT: "viewer"}, None)
    assert viewer == {
        "tools.search",
        "tools.invoke",
        "session.whoami",
        "runs.get",
        "runs.list",
        "allocations.wait",
    }
    contributor = _expected_agent_catalog({_PROJECT: "contributor"}, None)
    assert contributor == viewer | {"runs.create", "allocations.request", "systems.provision"}
    assert viewer < contributor
    # Pin the two remaining _ROLE_SUBJECTS shapes against the literal contributor set. Their
    # expectation otherwise runs through the same role_satisfies chain the server uses, so a
    # role-ordering regression would move both sides of those live rows together.
    assert _expected_agent_catalog({_PROJECT: "operator"}, None) == contributor
    assert _expected_agent_catalog({_PROJECT: "admin"}, None) == contributor
    # The tool the live tier reaches through the gateway has to stay outside the core set,
    # or its absence from an agent's catalog stops being the thing that test proves.
    assert _UNADVERTISED_TOOL not in CORE_TOOLS
    # Holding a platform role does not buy the contributor-gated core tools: the RBAC rule is
    # ADR-0148 §1's conservative union, as clipped to CORE_TOOLS by ADR-0268 §4.
    assert _expected_agent_catalog({_PROJECT: "viewer"}, ["platform_auditor"]) == viewer


@pytest.mark.oidc_issuer
def test_oidc_issuer_tier_mints_and_verifies_claim_shapes() -> None:
    """The gate (ADR-0044): the issuer mints nested roles + platform_roles into the access
    token; the real JWTVerifier accepts; roles_from_claims parses; wrong-aud rejects."""
    issuer = require_issuer()

    async def _run() -> None:
        verifier = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience=issuer.audience
        )
        wrong_aud = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience="not-kdive"
        )
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            verified = await verifier.verify_token(token)
            assert verified is not None, f"real verifier rejected {subject}'s token"
            assert verified.claims["roles"] == roles  # nested object survived
            parsed = roles_from_claims(verified.claims)
            assert parsed == {p: Role(r) for p, r in roles.items()}
            if platform_roles is not None:
                assert verified.claims["platform_roles"] == platform_roles  # flat array
            assert await wrong_aud.verify_token(token) is None  # verifier enforces aud

    asyncio.run(_run())


async def _invoke_through_gateway(
    base_url: str, token: str, tool: str, subject: str
) -> ToolResponse:
    """Call ``tool`` through ``tools.invoke`` over HTTP and parse the inner envelope.

    Not routed through :meth:`LiveStackClient.call_tool`: its ``name`` parameter is not
    positional-only, so the gateway's own ``name`` argument cannot be passed through it.
    ``tools.invoke`` returns the inner tool's structured content verbatim, so the payload
    parsed here is ``resources.list``'s own ``ToolResponse`` dump.

    ``raise_on_error=False`` for the same reason ``LiveStackClient.call_tool`` passes it:
    ``tools.invoke`` re-raises anything that is not a ``CategorizedError``, so a degraded
    dependency inside the inner tool would otherwise surface as a bare fastmcp ``ToolError``
    with neither the tool name nor the role under test attached.
    """
    transport = StreamableHttpTransport(url=base_url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        result = await client.call_tool(
            "tools.invoke", {"name": tool, "arguments": {}}, raise_on_error=False
        )
    if getattr(result, "is_error", False):
        raise LiveStackToolError(tool, f"as {subject}: {_tool_error_text(result)}")
    payload = result.structured_content
    assert payload is not None, f"{tool} through tools.invoke returned no structured content"
    return ToolResponse.model_validate(payload)


@pytest.mark.live_stack
def test_live_stack_tier_agent_catalog_is_gateway_clipped_over_http() -> None:
    """Over HTTP against a host-run server: the agent-profile catalog per role, and a tool
    the catalog omits reached through the gateway.

    Tokens carry no ``azp``, so every connection here takes the ``AGENT_GATEWAY`` profile
    and ``tools/list`` returns exactly ``rbac_visible & CORE_TOOLS`` (ADR-0268 §4,
    ADR-0456). ``resources.list`` is therefore absent, and ADR-0268's consequence — a
    non-core tool stays reachable — is what makes that acceptable, so this asserts the
    reach through ``tools.invoke`` rather than only the absence. The ``tools.search`` half
    of that consequence is not covered here; it belongs with the live-tier sweep.
    """
    issuer = require_issuer()
    base_url = require_stack()

    async def _run() -> None:
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            async with LiveStackClient.over_http(base_url, token) as client:
                names = set(await client.list_tools())
            # The catalog is the gateway clip, so _UNADVERTISED_TOOL's absence follows from
            # this equality; the sibling in-memory test pins it out of CORE_TOOLS.
            assert names == _expected_agent_catalog(roles, platform_roles), subject
            envelope = await _invoke_through_gateway(base_url, token, _UNADVERTISED_TOOL, subject)
            assert envelope.error_category is None, (
                f"{_UNADVERTISED_TOOL} through tools.invoke failed for {subject}: "
                f"{envelope.error_category} {envelope.detail}"
            )
            # The envelope has to be the inner tool's, not one tools.invoke built itself:
            # resources.list answers with ToolResponse.collection("resources", "ok", ...).
            assert (envelope.object_id, envelope.status) == ("resources", "ok"), subject

    asyncio.run(_run())


@pytest.mark.live_stack
def test_live_stack_tier_list_tools_is_rbac_scoped() -> None:
    """list_tools is filtered per connection (#506, ADR-0148): a viewer-only token sees a
    strictly smaller catalog than a privileged token, and the gated tools it cannot invoke
    are absent. This is the transport-level proof that the verified token resolves inside
    the ``on_list_tools`` hook over real HTTP — the filter never fires under the in-memory
    transport (which carries no token), so the live tier is the only place it is provable.

    Both tokens carry the operator CLI's OIDC ``azp`` so the connection takes the
    ``OPERATOR_DIRECT`` profile and ``tools/list`` returns the full RBAC-visible catalog
    (ADR-0456). Under the agent profile the gateway clips both catalogs to ``CORE_TOOLS``
    first, which would leave the RBAC filter's own effect unobservable — the sibling test
    above covers that profile.
    """
    issuer = require_issuer()
    base_url = require_stack()
    cli_client_id = config.require(CLI_CLIENT_ID)

    async def _names(
        subject: str, roles: dict[str, str], platform_roles: list[str] | None
    ) -> set[str]:
        token = mint_token(
            issuer,
            subject=subject,
            projects=[_PROJECT],
            roles=roles,
            platform_roles=platform_roles,
            client_id=cli_client_id,
        )
        async with LiveStackClient.over_http(base_url, token) as client:
            return set(await client.list_tools())

    async def _run() -> None:
        viewer = await _names("viewer-scope", {_PROJECT: "viewer"}, None)
        privileged = await _names(
            "admin-scope", {_PROJECT: "admin"}, ["platform_operator", "platform_admin"]
        )

        # The operator profile is in force, not the gateway's CORE_TOOLS clip. Three causes
        # redden this line — a server whose KDIVE_CLI_CLIENT_ID differs from this process's, a
        # server-side config read that raised (the middleware falls back to the agent profile
        # without saying so), and a genuine profile-resolution defect — so the message names
        # the one an operator can act on first. It also passes vacuously against a server
        # running with KDIVE_MCP_TOOL_GATEWAY off, since the middleware clips only when the
        # gateway is on as well; the sibling test's set equality is what pins the toggle on.
        assert not viewer <= CORE_TOOLS, (
            f"viewer catalog was clipped: the server did not resolve azp={cli_client_id!r} as "
            "the operator CLI — check that the server process's KDIVE_CLI_CLIENT_ID matches "
            "this one"
        )
        # Public + viewer-gated reads are advertised to the viewer.
        assert {"projects.list", "jobs.wait", "systems.list"} <= viewer
        # Operator/admin/platform-gated tools are hidden from the viewer.
        for hidden in ("allocations.request", "control.force_crash", "ops.reconcile_now"):
            assert hidden not in viewer, f"{hidden} leaked into the viewer catalog"
        # The privileged token sees them, and the viewer catalog is a strict subset.
        assert {"allocations.request", "control.force_crash", "ops.reconcile_now"} <= privileged
        assert viewer < privileged

    asyncio.run(_run())
