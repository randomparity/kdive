"""Explicit dependencies for deterministic superset MCP schema generation."""

from kdive.worker_lifecycle.contracts import WorkerDeathVerifier


class CatalogWorkerDeathVerifier(WorkerDeathVerifier):
    """Registration marker including conditionally deployed recovery schemas."""
