"""Generalized request-idempotency for object-creating / job-enqueuing mutations (ADR-0193).

Stores the returned :class:`~kdive.mcp.responses.ToolResponse` envelope under
``(principal, key)`` in the shared ``idempotency_keys`` table so a repeated key replays the
identical envelope for any object kind. Generalizes the allocation-only
``services/allocation/idempotency.py`` (ADR-0040 §3) without a schema change.
"""
