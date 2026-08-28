"""Generate the coverage-census rows by introspecting the live FastMCP app.

Mirrors the ADR-0047 doc guard's app-build path (null pool + local-keypair verifier;
no DB, no OIDC) so the static grid columns cannot drift from the real tool surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.assembly import ProcessAssembly
from kdive.mcp.assembly.app import build_app_from_assembly
from kdive.mcp.dev_harness import AUDIENCE, ISSUER, make_keypair
from kdive.mcp.tools import _docmeta
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from kdive.store.objectstore import ObjectStore


@dataclass(frozen=True)
class CensusRow:
    tool: str
    plane: str
    maturity: str
    annotation: str  # "read_only" | "mutating" | "destructive"
    destructive_member: bool


def _annotation(tool: FunctionTool) -> str:
    ann = tool.annotations
    if ann and ann.destructiveHint:
        return "destructive"
    if ann and ann.readOnlyHint:
        return "read_only"
    return "mutating"


def _build_tools() -> list[FunctionTool]:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    registry = SecretRegistry()
    object_stores = ObjectStoreAssembly(store=cast(ObjectStore, object()))
    process = ProcessAssembly(
        object_stores,
        ProviderComposition(secret_registry=registry, object_store=object_stores.store),
    )
    app = build_app_from_assembly(
        pool,
        verifier=verifier,
        process_assembly=process,
    )
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def generate_rows() -> list[CensusRow]:
    rows: list[CensusRow] = []
    for tool in _build_tools():
        meta = tool.meta or {}
        rows.append(
            CensusRow(
                tool=tool.name,
                plane=tool.name.split(".", 1)[0],
                maturity=str(meta.get("maturity", "")),
                annotation=_annotation(tool),
                destructive_member=tool.name in _docmeta.DESTRUCTIVE_TOOLS,
            )
        )
    rows.sort(key=lambda r: r.tool)
    return rows
