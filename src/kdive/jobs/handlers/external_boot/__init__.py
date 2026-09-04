"""Executable job surface for the external-boot lifecycle operations (ADR-0593).

The seven operations #2118 names ride the existing ``boot`` and ``teardown`` job kinds, selected by
an ``ExternalBootAuthorityMarkerV1`` on the payload, because ``allocate_external_boot_authority``
refuses unless the job's kind is exactly one of those two — a new ``JobKind`` member would be
unallocatable rather than merely undesirable. One shared router, injected as a required keyword
into the runs and systems registrars, diverts a marked job to a per-operation registry so it can
never reach the handler that boots a Run or tears a System down.

``materialize`` and ``prepare`` are dispositioned *prepared-before-admission*: the schema admits an
``activate`` allocation only from an activation state that already carries both ``materialization``
and ``recovery_point``, so the activate job cannot be what records them. They are preconditions the
handlers verify, never operations they perform.

This package imports no provider-specific module: the port comes from
``ProviderRuntime.external_boot`` and the ``provider_kind`` literals are data.
"""

from __future__ import annotations

from kdive.jobs.handlers.external_boot.operations import (
    DuplicateExternalBootHandler,
    ExternalBootOperationHandler,
    ExternalBootOperations,
)
from kdive.jobs.handlers.external_boot.ports import (
    EXTERNAL_BOOT_AUTHORITY_MARKER_KEY,
    ExternalBootAuthorityAcknowledger,
    ExternalBootHandlerPorts,
)
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked

__all__ = [
    "EXTERNAL_BOOT_AUTHORITY_MARKER_KEY",
    "DuplicateExternalBootHandler",
    "ExternalBootAuthorityAcknowledger",
    "ExternalBootHandlerPorts",
    "ExternalBootOperationHandler",
    "ExternalBootOperations",
    "build_operations",
    "route_marked",
]
