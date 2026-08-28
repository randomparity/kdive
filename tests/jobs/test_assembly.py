"""Worker handler registration assembly tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pydantic import SecretStr

from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS, RETIRED_JOB_KINDS, JobKind
from kdive.jobs.assembly import WorkerHandlerAssembly, register_all_handlers
from kdive.jobs.capture_operations.supervisor import CaptureOperationSupervisor
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from tests.support.object_store import INERT_OBJECT_STORE


def test_register_all_handlers_registers_active_and_no_retired_job_kinds() -> None:
    registry = HandlerRegistry()
    credential = SecretStr("worker-test-incarnation-credential")
    assembly = WorkerHandlerAssembly(
        resolver=ProviderResolver({}),
        incarnation_credential=credential,
        secret_registry=SecretRegistry(),
        object_stores=ObjectStoreAssembly(store=INERT_OBJECT_STORE),
        capture_supervisor=cast(
            CaptureOperationSupervisor,
            SimpleNamespace(credential=credential),
        ),
        worker_check_builders={},
    )

    register_all_handlers(registry, assembly)

    registered = frozenset(kind for kind in JobKind if registry.get(kind) is not None)
    assert registered == ACTIVE_JOB_KINDS
    assert registered.isdisjoint(RETIRED_JOB_KINDS)
