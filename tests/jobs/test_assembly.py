"""Worker handler registration assembly tests."""

from __future__ import annotations

import asyncio
import json
import ssl
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS, RETIRED_JOB_KINDS, JobKind
from kdive.jobs.assembly import WorkerHandlerAssembly, register_all_handlers
from kdive.jobs.capture_operations.supervisor import CaptureOperationSupervisor
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from tests.support.object_store import INERT_OBJECT_STORE


@pytest.mark.anyio
async def test_worker_routes_borrow_their_assembly_and_process_routes_remain_empty(monkeypatch):
    from kdive.assembly import ProcessAssembly
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.jobs import authority_sender
    from kdive.jobs.assembly import build_worker_handler_assembly
    from kdive.providers.assembly import composition as providers
    from kdive.providers.remote_libvirt import composition as remote
    from kdive.providers.remote_libvirt.config import (
        RemoteAuthorityBinding,
        RemoteLibvirtConfig,
        TlsCertRefs,
    )

    config = RemoteLibvirtConfig(
        uri="qemu+tls://example.invalid/system",
        cert_refs=TlsCertRefs("cert", "key", "ca"),
        concurrent_allocation_cap=1,
        authority=RemoteAuthorityBinding("authority-a", "192.0.2.1", 9443, "ca", "cert", "key"),
    )
    monkeypatch.setattr(providers, "_remote_libvirt_enabled", lambda _: True)
    monkeypatch.setattr(remote, "remote_config_for_resource", lambda _: config)
    resolved = []

    def tls(binding, backend):
        resolved.append(binding)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(authority_sender, "_resolve_tls_material", tls)
    owner = providers.ProviderComposition(
        secret_registry=SecretRegistry(), object_store=INERT_OBJECT_STORE
    )
    process = ProcessAssembly(ObjectStoreAssembly(INERT_OBJECT_STORE), owner)
    assert (
        owner.build_provider_resolver()
        .resolve(ResourceKind.REMOTE_LIBVIRT)
        .for_resource("resource-a")
        .authority
        is None
    )
    assert resolved == []
    workers = [
        build_worker_handler_assembly(
            process_assembly=process, incarnation_credential=SecretStr(value)
        )
        for value in ("worker-original-credential", "worker-replacement-credential")
    ]
    senders = [
        worker.resolver.resolve(ResourceKind.REMOTE_LIBVIRT).for_resource("resource-a").authority
        for worker in workers
    ]
    assert all(sender is not None for sender in senders)
    assert resolved == [config.authority, config.authority]
    seen = []

    async def request(self, envelope, *, deadline):
        seen.append(json.loads(envelope)["credential"])
        return b'{"status":"ok","value":{"schema":"external-boot-authority-health-v1"}}'

    monkeypatch.setattr(authority_sender._AuthorityNetworkTransport, "_request_frame", request)
    for sender in senders:
        assert isinstance(sender, authority_sender.AuthorityRequestSender)
        await sender.health(deadline=asyncio.get_running_loop().time() + 1)
        for node in (sender, sender._transport):
            assert all(not isinstance(getattr(node, slot), SecretStr) for slot in node.__slots__)
    assert seen == [worker.incarnation_credential.get_secret_value() for worker in workers]
    assert (
        owner.build_provider_resolver()
        .resolve(ResourceKind.REMOTE_LIBVIRT)
        .for_resource("resource-a")
        .authority
        is None
    )


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
