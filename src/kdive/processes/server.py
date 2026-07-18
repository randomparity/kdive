"""MCP server process runner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool
from starlette.middleware import Middleware

from kdive.db.pool import create_pool
from kdive.mcp.middleware.bare_bearer_hint import BareBearerHintMiddleware
from kdive.processes.runtime import HEARTBEAT_STALE_SECONDS, run_process_runtime

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry

HTTP_KEEPALIVE_S = 65.0


def server_uvicorn_config() -> dict[str, Any]:
    return {"timeout_keep_alive": HTTP_KEEPALIVE_S}


def server_http_middleware() -> list[Middleware]:
    """ASGI middleware injected ahead of FastMCP's vendored auth endpoint (ADR-0380).

    `BareBearerHintMiddleware` turns a bare-JWT `Authorization` header (no `Bearer `
    scheme prefix) into an accurate 401 before the vendored path emits its misleading
    "token invalid/expired" error.
    """
    return [Middleware(BareBearerHintMiddleware)]


async def run_server(
    host: str, port: int, secret_registry: SecretRegistry, telemetry: Telemetry
) -> None:
    from kdive.health.probe import HealthProbe
    from kdive.health.processes.server import build_oidc_ping, build_postgres_ping
    from kdive.health.server_checks import build_server_checks
    from kdive.mcp.assembly.app import build_app
    from kdive.store.objectstore import object_store_from_env

    def build_probe(pool: AsyncConnectionPool) -> HealthProbe:
        return HealthProbe(
            checks=build_server_checks(
                postgres_ping=build_postgres_ping(pool),
                object_store_factory=object_store_from_env,
                oidc_ping=build_oidc_ping(),
            )
        )

    async def serve_mcp(
        pool: AsyncConnectionPool, heartbeat: Heartbeat, probe: HealthProbe
    ) -> None:
        del heartbeat, probe
        app = build_app(pool, secret_registry=secret_registry)
        await app.run_async(
            transport="http",
            host=host,
            port=port,
            uvicorn_config=server_uvicorn_config(),
            middleware=server_http_middleware(),
        )

    await run_process_runtime(
        process="server",
        pool=create_pool(),
        secret_registry=secret_registry,
        telemetry=telemetry,
        heartbeat_stale_after=HEARTBEAT_STALE_SECONDS,
        probe_builder=build_probe,
        body=serve_mcp,
        tick_heartbeat=True,
    )
