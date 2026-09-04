"""The marker-``operation`` to handler registry a marked job is dispatched through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import ExternalBootAuthorityMarkerV1, ExternalBootAuthoritySuccessV1
from kdive.jobs.payloads import (
    ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS,
    BootPayload,
    TeardownPayload,
    load_payload,
)

__all__ = [
    "DuplicateExternalBootHandler",
    "ExternalBootOperationHandler",
    "ExternalBootOperations",
]

type ExternalBootOperationHandler = Callable[
    [AsyncConnection, Job, ExternalBootAuthorityMarkerV1],
    Awaitable[ExternalBootAuthoritySuccessV1],
]
"""An operation either returns a **success** result or raises ``ExternalBootAuthorityFailure``.

The success subclass rather than the base ``ExternalBootAuthorityResultV1`` is deliberate.
``kdive.jobs.worker._commit_external_result`` dispatches on ``isinstance``: a success goes to
``queue.complete_external_boot``, a failure to ``queue.fail_external_boot``, and a bare base
instance is logged as an "untyped result variant" and written nowhere — so the job keeps its lease
and wedges ``running``. Typing the seam as the base class let exactly that ship and type-check
clean; typing it as the subclass makes the same mistake a ``ty`` error.
"""


class DuplicateExternalBootHandler(RuntimeError):
    """A second handler was registered for an operation that already has one."""


def _configuration_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, terminal=True)


class ExternalBootOperations:
    """A one-handler-per-operation registry, keyed by the marker's ``operation``."""

    def __init__(self) -> None:
        self._handlers: dict[str, ExternalBootOperationHandler] = {}

    def register(self, operation: str, handler: ExternalBootOperationHandler) -> None:
        """Bind ``handler`` to ``operation``.

        Raises:
            ValueError: ``operation`` is outside ``ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS``.
                ``deadline`` and ``recovery-attempt`` are mid-operation commits that leave the
                ``jobs`` row ``running`` on purpose, and ``fail`` is a result carrier; none is an
                admission a job may be enqueued for.
            DuplicateExternalBootHandler: ``operation`` already has a handler.
        """
        if operation not in ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS:
            raise ValueError(f"{operation!r} is not an enqueueable external-boot operation")
        if operation in self._handlers:
            raise DuplicateExternalBootHandler(f"a handler is already registered for {operation!r}")
        self._handlers[operation] = handler

    def get(self, operation: str) -> ExternalBootOperationHandler | None:
        return self._handlers.get(operation)

    def registered_operations(self) -> frozenset[str]:
        return frozenset(self._handlers)

    async def run(self, conn: AsyncConnection, job: Job) -> ExternalBootAuthoritySuccessV1:
        """Decode ``job``'s marker and dispatch to the handler bound to its operation.

        Decoding happens here rather than in the router, so the router needs no decoder of its own
        and a malformed marker fails closed inside this registry instead of reaching the handler
        that boots a Run or tears a System down.
        """
        model = BootPayload if job.kind is JobKind.BOOT else TeardownPayload
        payload = load_payload(job, model)
        marker = payload.external_boot_authority_v1
        if marker is None:
            raise _configuration_error(
                f"{job.kind.value} job {job.id} was routed as marked but carries no marker"
            )
        handler = self.get(marker.operation)
        if handler is None:
            raise _configuration_error(
                f"no external-boot handler is registered for operation {marker.operation!r}"
            )
        return await handler(conn, job, marker)
