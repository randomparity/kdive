"""The external-boot operations registry and its production wiring (charter criterion 4).

Criterion 4 has two halves and they are asserted separately, because one is much weaker than the
other: that ``build_operations`` binds each enqueueable operation once, and that the registry the
**production** builder returns resolves each marker ``operation`` to exactly one handler. The
second is what proves ``register_all_handlers`` passed the *same* registry to both registrars.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from psycopg import AsyncConnection
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS, RETIRED_JOB_KINDS, Job, JobKind
from kdive.jobs import assembly as jobs_assembly
from kdive.jobs.assembly import (
    WorkerHandlerAssembly,
    build_production_handler_registry,
    register_all_handlers,
)
from kdive.jobs.capture_operations.supervisor import CaptureOperationSupervisor
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.handlers.external_boot.operations import (
    DuplicateExternalBootHandler,
    ExternalBootOperationHandler,
    ExternalBootOperations,
)
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.runs import registrar as runs_registrar
from kdive.jobs.models import (
    DuplicateHandler,
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
    HandlerRegistry,
)
from kdive.jobs.payloads import ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from tests.support.object_store import INERT_OBJECT_STORE

# The six names as literals. Deliberately not ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS: parametrizing
# over the constant would compare the constant to itself one level up, because the constant is
# what both ExternalBootOperations.register and the payload validator gate on. The constant's own
# contents are pinned once, in tests/jobs/test_external_boot_payloads.py.
OPERATIONS = ["activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"]

CREDENTIAL = SecretStr("worker-test-incarnation-credential")


def _stub_assembly() -> WorkerHandlerAssembly:
    """The assembly a unit test can build: real ports except the ones needing process config."""
    return WorkerHandlerAssembly(
        resolver=ProviderResolver({}),
        incarnation_credential=CREDENTIAL,
        secret_registry=SecretRegistry(),
        object_stores=ObjectStoreAssembly(store=INERT_OBJECT_STORE),
        capture_supervisor=cast(CaptureOperationSupervisor, SimpleNamespace(credential=CREDENTIAL)),
        worker_check_builders={},
    )


def _ports() -> ExternalBootHandlerPorts:
    return ExternalBootHandlerPorts(
        resolver=ProviderResolver({}),
        incarnation_credential=CREDENTIAL,
        secret_registry=SecretRegistry(),
    )


def test_build_operations_binds_each_enqueueable_operation_once() -> None:
    """The weaker half of criterion 4: ``build_operations`` is internally complete.

    This says nothing about whether the production registry reaches those handlers, which is what
    ``test_production_registry_resolves_every_operation_to_one_handler`` below asserts.
    """
    operations = build_operations(_ports())

    assert operations.registered_operations() == ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS


def test_second_registration_for_an_operation_raises() -> None:
    operations = ExternalBootOperations()
    handler = AsyncMock()
    operations.register("activate", handler)

    with pytest.raises(DuplicateExternalBootHandler, match="activate"):
        operations.register("activate", handler)


@pytest.mark.parametrize("operation", ["deadline", "recovery-attempt", "fail"])
def test_register_refuses_a_non_enqueueable_operation(operation: str) -> None:
    """``deadline``/``recovery-attempt`` are mid-operation commits; ``fail`` carries a result."""
    operations = ExternalBootOperations()

    with pytest.raises(ValueError, match=operation):
        operations.register(operation, AsyncMock())


def test_run_refuses_an_operation_this_registry_never_bound(
    make_marked_job: Callable[..., Job],
) -> None:
    """A registry missing one of the six refuses that job rather than dispatching it nowhere.

    Reachable only through an incomplete registry, not through a hand-built payload: the payload
    validator already rejects any marker outside the enqueueable six, so ``deadline`` cannot
    survive ``load_payload``. This is the defensive branch for a ``build_operations`` that stopped
    binding an operation.
    """
    operations = ExternalBootOperations()
    for operation in OPERATIONS:
        if operation != "cleanup":
            operations.register(operation, AsyncMock())
    job = make_marked_job("cleanup")

    with pytest.raises(CategorizedError, match="cleanup") as excinfo:
        asyncio.run(operations.run(cast(AsyncConnection, None), job))

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.terminal is True


def test_run_refuses_a_job_whose_payload_carries_no_marker(
    make_marked_job: Callable[..., Job],
) -> None:
    """``run`` is only reached for a marked job, so an absent marker is a wiring fault."""
    operations = build_operations(_ports())
    job = make_marked_job("activate")
    job = job.model_copy(update={"payload": {"run_id": job.payload["run_id"]}})

    with pytest.raises(CategorizedError, match="marker") as excinfo:
        asyncio.run(operations.run(cast(AsyncConnection, None), job))

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.terminal is True


def test_handler_registry_binds_boot_and_teardown_exactly_once() -> None:
    """The "no duplicate registration" half of criterion 4.

    Asserting only that ``get(kind)`` is non-``None`` would pass under a double registration if one
    silently won, so this also asserts a further ``register`` on the same kind raises.
    """
    registry = HandlerRegistry()
    register_all_handlers(registry, _stub_assembly())

    assert registry.get(JobKind.BOOT) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    for kind in (JobKind.BOOT, JobKind.TEARDOWN):
        with pytest.raises(DuplicateHandler):
            registry.register(kind, AsyncMock())


def test_register_all_handlers_still_covers_every_active_kind() -> None:
    """Wrapping two kinds in the router must not drop or add a registration."""
    registry = HandlerRegistry()
    register_all_handlers(registry, _stub_assembly())

    registered = frozenset(kind for kind in JobKind if registry.get(kind) is not None)
    assert registered == ACTIVE_JOB_KINDS
    assert registered.isdisjoint(RETIRED_JOB_KINDS)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_production_registry_resolves_every_operation_to_one_handler(
    operation: str,
    make_marked_job: Callable[..., Job],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4 asserted **through** ``build_production_handler_registry``, not around it.

    ``HandlerRegistry`` exposes only ``register`` and ``get(kind)``, and the operations registry is
    captured in the router closure, so it cannot be read off the returned object. The test drives
    it instead: it builds a marked job, fetches the handler the production registry returned for
    that job's ``JobKind``, awaits it, and asserts exactly one operation handler ran and it was the
    one bound to that operation. Because every one of the six is a recording double, "exactly one"
    is asserted against the recorder rather than inferred.

    Only ``build_production_worker_handler_assembly`` is stubbed, and only because it reads process
    configuration a unit test does not have (it raises ``KDIVE_S3_ENDPOINT_URL is not set``). Every
    line under test — ``build_production_handler_registry`` → ``build_handler_registry`` →
    ``register_all_handlers`` → both registrars → ``route_marked`` → the operations registry — is
    the production path, so this is the entry point the criterion names rather than a substitute
    for it.

    Three assertions, and each exists because a bite proof showed the others do not cover it:

    - ``build_calls == 1`` is what pins **one shared** registry. An earlier draft claimed the
      dispatch assertion proved it; injecting a second ``build_operations`` call for the systems
      registrar left that draft green, because a per-call recorder registry appending to one list
      is indistinguishable from a shared one. The claim was false and this assertion is what makes
      it true.
    - ``ordinary == []`` is what pins that the **marked** job never reached ``boot_handler`` or
      ``teardown_handler``. Without the ordinary doubles, un-wrapping the router made the real
      handler run against a ``None`` connection and the test died with an ``AttributeError`` — a
      crash indistinguishable from a broken fixture, not evidence.
    - ``calls == [operation]`` is the dispatch itself, asserted against a recorder rather than
      inferred, so "exactly one handler ran" is observed.
    """
    calls: list[str] = []
    ordinary: list[str] = []
    built: list[ExternalBootOperations] = []
    shared = ExternalBootOperations()
    for name in OPERATIONS:
        shared.register(name, _recorder(name, calls))

    def recording_build_operations(ports: ExternalBootHandlerPorts) -> ExternalBootOperations:
        del ports
        built.append(shared)
        return shared

    async def ordinary_double(label: str) -> str:
        ordinary.append(label)
        return label

    monkeypatch.setattr(
        jobs_assembly, "build_production_worker_handler_assembly", lambda **_: _stub_assembly()
    )
    monkeypatch.setattr(jobs_assembly.external_boot, "build_operations", recording_build_operations)
    monkeypatch.setattr(
        runs_registrar, "boot_handler", lambda *_a, **_kw: ordinary_double("boot_handler")
    )
    monkeypatch.setattr(
        systems_handlers, "teardown_handler", lambda *_a, **_kw: ordinary_double("teardown_handler")
    )

    registry = build_production_handler_registry(
        secret_registry=SecretRegistry(), incarnation_credential=CREDENTIAL, pool=None
    )
    job = make_marked_job(operation)
    handler = registry.get(job.kind)
    assert handler is not None

    asyncio.run(handler(cast(AsyncConnection, None), job))

    assert len(built) == 1, "register_all_handlers must build one registry and share it"
    assert ordinary == []
    assert calls == [operation]


def _recorder(name: str, calls: list[str]) -> ExternalBootOperationHandler:
    """A handler that records only its own name, so "exactly one ran" is asserted, not inferred."""

    async def handler(
        _conn: AsyncConnection, _job: Job, _marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthoritySuccessV1:
        calls.append(name)
        return cast(ExternalBootAuthoritySuccessV1, None)

    return handler
