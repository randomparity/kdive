"""Per-job provider-kind tag for provider-op RED metrics (ADR-0191 F).

A provider-backed job handler tags the in-flight job with its provider kind where it
already resolves the runtime; the worker's per-job telemetry reads the tag on close to emit
``kdive.provider.op.*`` with a ``provider`` label. A contextvar (not a handler signature
change) carries the tag: the handler runs in the same task as the worker's job span, so a
value set in the handler is visible when the span closes. The worker clears it per job so a
provider-backed job never leaks its kind onto a following provider-less job.
"""

from __future__ import annotations

from contextvars import ContextVar

_provider_kind: ContextVar[str | None] = ContextVar("kdive_provider_kind", default=None)


def set_provider_kind(value: str) -> None:
    _provider_kind.set(value)


def clear_provider_kind() -> None:
    _provider_kind.set(None)


def take_provider_kind() -> str | None:
    value = _provider_kind.get()
    _provider_kind.set(None)
    return value
