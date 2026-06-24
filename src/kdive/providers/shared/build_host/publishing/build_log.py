"""Persist a failed internal build's captured output as a `build-log` artifact (ADR-0238).

On a build-step failure the orchestrator attaches the captured ``make``/``olddefconfig`` output
to the raised :class:`~kdive.domain.errors.CategorizedError` under ``details["build_log"]``. The
builder calls :func:`persist_build_log` to PUT that text — already redacted and tail-capped at
capture — under the Run-keyed object key as a ``REDACTED`` artifact so ``artifacts.get`` can serve
it. The DB row is registered by the worker (which holds the connection); this module owns only the
object write.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from kdive.artifacts.storage import ArtifactWriteRequest
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host.publishing.artifact_publish import StorePort

_log = logging.getLogger(__name__)

BUILD_LOG_NAME = "build-log"
BUILD_LOG_RETENTION_CLASS = "build-log"
# The error detail key carrying the captured build output up to the builder (set by the
# orchestrator's `build_failure`), and the one carrying the stored object key down to the worker.
BUILD_LOG_DETAIL = "build_log"
BUILD_LOG_ARTIFACT_DETAIL = "build_log_artifact"


def persist_build_log(store: StorePort, run_id: UUID, output: str, *, tenant: str) -> str | None:
    """PUT ``output`` as a Run-owned ``REDACTED`` ``build-log`` object; return its key.

    The key is Run-keyed (``<tenant>/runs/<run_id>/build-log``), so a re-capture on a BUILD-job
    retry overwrites the same object rather than accumulating objects. ``output`` is already
    redacted and tail-capped by the capture seam; it is stored ``REDACTED`` so the redaction gate
    on ``artifacts.get`` will serve it.

    Args:
        store: The object store to PUT into.
        run_id: The failing Run; owns the artifact (``owner_kind='runs'``).
        output: The redacted, capped build output. Empty/blank input is a no-op.
        tenant: The object-key tenant prefix.

    Returns:
        The stored object key, or ``None`` when ``output`` is empty (no object written).
    """
    if not output.strip():
        return None
    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant=tenant,
            owner_kind="runs",
            owner_id=str(run_id),
            name=BUILD_LOG_NAME,
            data=output.encode("utf-8"),
            sensitivity=Sensitivity.REDACTED,
            retention_class=BUILD_LOG_RETENTION_CLASS,
        )
    )
    return stored.key


def build_workspace_capturing_log(
    build_workspace: Callable[[], object],
    store: StorePort,
    run_id: UUID,
    *,
    tenant: str,
) -> None:
    """Run ``build_workspace``; on a build failure carrying captured output, persist the log.

    Wraps the orchestrator's ``build_workspace`` call so a ``BUILD_FAILURE`` whose
    ``details[BUILD_LOG_DETAIL]`` carries captured ``make``/``olddefconfig`` output PUTs that text
    as a ``build-log`` artifact and re-raises the *same* error with the stored object key added to
    ``details[BUILD_LOG_ARTIFACT_DETAIL]`` for the worker to register. A persistence failure is
    logged and swallowed so the original build failure always propagates — a build-log outage must
    never mask or reshape the build error. Failures carrying no captured output propagate unchanged.
    """
    try:
        build_workspace()
    except CategorizedError as exc:
        output = exc.details.get(BUILD_LOG_DETAIL)
        if exc.category is not ErrorCategory.BUILD_FAILURE or not isinstance(output, str):
            raise
        try:
            key = persist_build_log(store, run_id, output, tenant=tenant)
        except CategorizedError:
            _log.warning("failed to persist build-log for run %s", run_id, exc_info=True)
            raise exc from None
        if key is not None:
            exc.details[BUILD_LOG_ARTIFACT_DETAIL] = key
        raise
