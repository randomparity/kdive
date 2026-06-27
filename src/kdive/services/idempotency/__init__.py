"""Generalized request-idempotency for object-creating / job-enqueuing mutations (ADR-0193).

Stores a transport-neutral :class:`~kdive.services.idempotency.envelope.StoredResult`
under ``(principal, key, kind)`` in the shared ``idempotency_keys`` table so a repeated key
replays the same JSON result for the same operation kind. MCP envelope conversion lives at
the transport boundary in :mod:`kdive.mcp.tools._idempotency`. Generalizes the
allocation-only ``services/allocation/idempotency.py`` (ADR-0040 §3) without a schema change.
"""
