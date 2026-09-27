"""Generate the coverage-census rows by introspecting the live FastMCP app.

Mirrors the ADR-0047 doc guard's app-build path (null pool + local-keypair verifier;
no DB, no OIDC) so the static grid columns cannot drift from the real tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import cast

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.assembly import ProcessAssembly
from kdive.mcp.assembly.app import build_app_from_assembly
from kdive.mcp.dev_harness import AUDIENCE, ISSUER, make_keypair
from kdive.mcp.exposure import required_scopes
from kdive.mcp.schema.schema_advertising import registered_tools
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
    configurations: tuple[str, ...] = ()
    parameters: dict[str, object] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()


def _annotation(tool: FunctionTool) -> str:
    ann = tool.annotations
    if ann and ann.destructiveHint:
        return "destructive"
    if ann and ann.readOnlyHint:
        return "read_only"
    return "mutating"


class _OfflineVerifier:
    def verify_dead(self, worker_incarnation: str) -> str | None:
        raise RuntimeError("the census must not invoke worker operations")


def _build_tools(*, recovery: bool = False) -> list[FunctionTool]:
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
        worker_death_verifier=_OfflineVerifier() if recovery else None,
    )
    return cast(list[FunctionTool], list(registered_tools(app)))


def generate_rows() -> list[CensusRow]:
    rows: dict[str, CensusRow] = {}
    for configuration in ("default", "recovery"):
        for tool in _build_tools(recovery=configuration == "recovery"):
            meta = tool.meta or {}
            row = CensusRow(
                tool=tool.name,
                plane=tool.name.split(".", 1)[0],
                maturity=str(meta.get("maturity", "")),
                annotation=_annotation(tool),
                destructive_member=tool.name in _docmeta.DESTRUCTIVE_TOOLS,
                parameters=tool.parameters,
                scopes=tuple(sorted(required_scopes(tool.name))),
            )
            previous = rows.get(tool.name)
            configurations = previous.configurations if previous else ()
            if previous and replace(previous, configurations=()) != row:
                raise ValueError("registered tool metadata differs between configurations")
            rows[tool.name] = replace(row, configurations=(*configurations, configuration))
    return sorted(rows.values(), key=lambda row: row.tool)
