"""Epic #1442 R2 collision guard (ADR-0422): no tool parameter may shadow a reserved CLI flag.

ADR-0421 generates a ``kdivectl`` verb per tool and derives each tool parameter to a
long-form flag by decision 2 (underscores to dashes, ``--`` prefix). ADR-0422 reduces #1445
to this build-time assertion: every registered tool's parameter, run through that derivation
rule, must NOT land on a reserved flag (``--json`` / ``--help`` / ``--yes`` plus the
defensively-reserved ``tool call`` tier flags). It passes today — no current parameter
collides, and the two ``force`` parameters derive to a non-reserved ``--force`` (ADR-0422) —
so its value is tripping a FUTURE tool that introduces a parameter named ``json``/``help``/
``yes``/``allow_mutating``/``allow_destructive``. ``test_flag_collision_guard_bites`` proves
the detector would catch exactly that.

Mirrors ``tests/mcp/core/test_tool_docs.py`` for building the live registry and enumerating
each tool's input-schema parameter names.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.cli.reserved_flags import RESERVED_CLI_FLAGS, derive_cli_flag
from kdive.mcp.assembly.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _build_tools() -> list[FunctionTool]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def _flag_collisions(params_by_tool: Mapping[str, Iterable[str]]) -> list[str]:
    """Return one offender message per tool parameter that derives to a reserved CLI flag.

    Pure over its argument so the guard runs against the live registry and the negative test
    runs against synthetic input, without needing a fake registered tool.
    """
    offenders: list[str] = []
    for tool_name in sorted(params_by_tool):
        for param in sorted(params_by_tool[tool_name]):
            flag = derive_cli_flag(param)
            if flag in RESERVED_CLI_FLAGS:
                offenders.append(
                    f"tool {tool_name!r} parameter {param!r} derives to reserved CLI flag {flag}"
                )
    return offenders


def _params_by_tool(tools: list[FunctionTool]) -> dict[str, set[str]]:
    return {t.name: set((t.parameters or {}).get("properties", {})) for t in tools}


TOOLS = _build_tools()


def test_no_tool_parameter_derives_to_a_reserved_cli_flag() -> None:
    offenders = _flag_collisions(_params_by_tool(TOOLS))
    assert not offenders, "tool parameters shadowing a reserved kdivectl flag:\n" + "\n".join(
        offenders
    )


def test_force_parameters_derive_to_a_non_reserved_flag() -> None:
    # ADR-0422: resources.deregister and runs.boot keep their `force` parameter, which derives
    # to `--force` — deliberately NOT in the reserved set (the global break-glass `--force` CLI
    # flag was retired by ADR-0421 decision 8), so it must pass the guard.
    by_name = _params_by_tool(TOOLS)
    for tool_name in ("resources.deregister", "runs.boot"):
        assert "force" in by_name[tool_name], f"{tool_name} lost its `force` parameter"
    assert derive_cli_flag("force") == "--force"
    assert "--force" not in RESERVED_CLI_FLAGS


def test_flag_collision_guard_bites() -> None:
    # Canary: the detector must catch a parameter that derives to a reserved flag, both a direct
    # name (`json` -> `--json`) and one exercising the underscore->dash rule
    # (`allow_mutating` -> `--allow-mutating`), while leaving clean params untouched.
    assert _flag_collisions({"ns.op": {"json", "run_id"}}) == [
        "tool 'ns.op' parameter 'json' derives to reserved CLI flag --json"
    ]
    assert _flag_collisions({"ns.op": {"allow_mutating"}}) == [
        "tool 'ns.op' parameter 'allow_mutating' derives to reserved CLI flag --allow-mutating"
    ]
    assert _flag_collisions({"ns.op": {"force", "run_id", "project"}}) == []


def test_reserved_set_covers_the_live_tier_flags() -> None:
    # The defensively-reserved tier flags must track their source of truth in passthrough, so a
    # rename there cannot silently drop them from the reserved set.
    from kdive.cli.passthrough import _FLAG_FOR_TIER

    assert set(_FLAG_FOR_TIER.values()) <= RESERVED_CLI_FLAGS
    assert {"--json", "--help", "--yes"} <= RESERVED_CLI_FLAGS
