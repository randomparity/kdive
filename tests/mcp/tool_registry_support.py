"""The live FastMCP tool registry, built once per caller for registry-wide guards.

Assembly needs a pool and a verifier but never opens either, so this is the service-test path:
no database and no OIDC environment. Kept out of the test modules that use it because more than
one guard reads the registry (documentation, ADR-0583 admission coverage) and a test module may
not import another.
"""

from __future__ import annotations

import asyncio
from typing import cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.mcp.assembly.schema_catalog import CatalogWorkerDeathVerifier
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def build_registered_tools() -> list[FunctionTool]:
    """Return every registered tool, as the concrete type carrying `.fn` / `.meta`."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(
        pool,
        verifier=verifier,
        secret_registry=SecretRegistry(),
        worker_death_verifier=CatalogWorkerDeathVerifier(),
    )
    # list_tools() is typed as Sequence[mcp.types.Tool] but the fastmcp runtime returns
    # list[FunctionTool] — cast to the concrete type so callers can reach .fn / .meta /
    # .annotations without type errors.
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))
