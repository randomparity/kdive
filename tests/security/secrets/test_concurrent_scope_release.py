"""Concurrent scoped register/release over one SecretRegistry (#1538, ADR-0550).

The property the worker's per-lane concurrency rests on. Since ADR-0550 a worker process runs
one in-flight job per accepted dispatch lane, and every job shares this one registry — so a
value two jobs both resolved must stay masked until the **last** of them releases its scope. If
``release`` evicted by scope rather than decrementing a refcount, the first job to finish would
unmask a credential the second is still using, and it would surface in logs, in redacted
response snippets, and in the persisted ``failure_context``.

The registry is reference-counted for exactly this reason; these cases are what stop a future
refactor from quietly removing that.
"""

from __future__ import annotations

import threading

from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_SHARED = "shared-credential-value"  # pragma: allowlist secret


def _masks(registry: SecretRegistry, value: str) -> bool:
    """Whether a redactor built now — as the response path does — still masks ``value``.

    The carrier is deliberately neutral prose. A `token=<value>` probe would be masked by the
    redactor's key=value heuristic whether or not the registry still holds the value, which
    would make every assertion below vacuously true in both directions.
    """
    return value not in Redactor(registry=registry).redact_text(f"guest console said {value} ok")


def test_one_holder_releasing_keeps_the_value_masked_for_the_other() -> None:
    registry = SecretRegistry()
    job_a, job_b = object(), object()
    registry.register(_SHARED, scope=job_a)
    registry.register(_SHARED, scope=job_b)

    registry.release(job_a)

    # Job B is still running with this credential resolved.
    assert _masks(registry, _SHARED)

    registry.release(job_b)
    assert not _masks(registry, _SHARED)


def test_the_value_is_released_only_once_every_holder_is_gone() -> None:
    registry = SecretRegistry()
    scopes = [object() for _ in range(5)]
    for scope in scopes:
        registry.register(_SHARED, scope=scope)

    for scope in scopes[:-1]:
        registry.release(scope)
        assert _masks(registry, _SHARED)

    registry.release(scopes[-1])
    assert not _masks(registry, _SHARED)


def test_a_process_global_registration_survives_every_scoped_release() -> None:
    # The worker's own incarnation credential is registered with scope=None and must outlive
    # every job that runs beside it (ADR-0012).
    registry = SecretRegistry()
    registry.register(_SHARED, scope=None)
    job = object()
    registry.register(_SHARED, scope=job)

    registry.release(job)

    assert _masks(registry, _SHARED)


def test_concurrent_register_and_release_never_drops_a_live_holder() -> None:
    """Threads, not just interleaved calls: the registry guards its refcount with a lock.

    One thread churns a scope while a second holds the value throughout. The held value must be
    masked at every observation — a lost update on the refcount would unmask it mid-run.
    """
    registry = SecretRegistry()
    holder = object()
    registry.register(_SHARED, scope=holder)
    unmasked_observations: list[int] = []
    barrier = threading.Barrier(2)

    def churn() -> None:
        barrier.wait()
        for _ in range(200):
            scope = object()
            registry.register(_SHARED, scope=scope)
            registry.release(scope)

    def observe() -> None:
        barrier.wait()
        for index in range(200):
            if not _masks(registry, _SHARED):
                unmasked_observations.append(index)

    threads = [threading.Thread(target=churn), threading.Thread(target=observe)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert unmasked_observations == []
    # And the long-lived holder is still the reason it is masked.
    assert _masks(registry, _SHARED)
    registry.release(holder)
    assert not _masks(registry, _SHARED)
