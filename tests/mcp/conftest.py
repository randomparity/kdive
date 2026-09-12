"""MCP test fixtures: re-export DB fixtures and a JWT-minting helper."""

from __future__ import annotations

from typing import Any

from kdive.mcp.dev_harness import AUDIENCE, ISSUER, make_keypair, mint  # noqa: F401

# Re-export the disposable-Postgres fixtures so DB-backed MCP tests can use them.
from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url  # noqa: F401

# Re-export the disposable-MinIO fixture so the upload-rootfs commit test can reach a
# real object store (the rootfs artifacts row is committed against it at provisioning).
from tests.store.conftest import minio_store  # noqa: F401


class FakeTool:
    """Minimal Tool stand-in for projection tests that exercise ``model_copy``."""

    def __init__(
        self, name: str, parameters: dict[str, Any], *, description: str | None = None
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters

    def model_copy(self, *, update: dict[str, Any]) -> FakeTool:
        return FakeTool(self.name, update["parameters"], description=self.description)
