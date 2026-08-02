"""Explicit dependencies for deterministic superset MCP schema generation."""


class CatalogWorkerDeathVerifier:
    """Non-executed verifier that includes conditionally deployed recovery schemas."""

    def verify_dead(self, worker_incarnation: str) -> str | None:
        raise RuntimeError("schema-catalog verifier must never execute")
