"""Bind the six enqueueable external-boot operations into one registry."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperations
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.models import ExternalBootAuthorityMarkerV1, ExternalBootAuthorityResultV1

__all__ = ["build_operations"]

_OPERATIONS = ("activate", "recover", "resolve-conflict", "release", "cleanup", "teardown")


def _unimplemented(operation: str):  # noqa: ANN202 - returns ExternalBootOperationHandler
    """Placeholder binding: the operation is registered but its body is not written yet."""

    async def handler(
        _conn: AsyncConnection, _job: Job, _marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthorityResultV1:
        raise CategorizedError(
            f"the {operation!r} external-boot handler is not implemented",
            category=ErrorCategory.CONFIGURATION_ERROR,
            terminal=True,
        )

    return handler


def build_operations(ports: ExternalBootHandlerPorts) -> ExternalBootOperations:
    """Build the operations registry, raising on a duplicate or non-enqueueable binding."""
    del ports
    operations = ExternalBootOperations()
    for operation in _OPERATIONS:
        operations.register(operation, _unimplemented(operation))
    return operations
