"""Offline MCP catalog shared by code and documentation generators (ADR-0836)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from psycopg_pool import AsyncConnectionPool

from kdive.assembly import ProcessAssembly
from kdive.mcp.assembly.app import build_app_from_assembly
from kdive.mcp.assembly.schema_catalog import CatalogWorkerDeathVerifier
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from kdive.store.objectstore import ObjectStore


def registry_tools() -> list[Any]:
    """Build the generator tool catalog without entering any runtime lifespan."""
    # Offline schema extraction: the app is built purely to read tool schemas. Neither the pool
    # nor the injected store is invoked, so generation does not depend on live infrastructure.
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = RSAKeyPair.generate()
    verifier = JWTVerifier(public_key=kp.public_key, issuer="https://gen.local", audience="kdive")
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
        worker_death_verifier=CatalogWorkerDeathVerifier(),
    )
    # The erased `list` cast is intentional: the generator only reads duck-typed
    # attributes (name/description/meta/annotations/parameters), so it needs none
    # of the concrete FunctionTool type the guard test casts to for `.fn`.
    return cast(list[Any], asyncio.run(app.list_tools()))
