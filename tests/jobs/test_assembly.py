"""Worker handler registration assembly tests."""

from __future__ import annotations

import asyncio
import json
import ssl
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import ACTIVE_JOB_KINDS, RETIRED_JOB_KINDS, JobKind
from kdive.jobs.assembly import WorkerHandlerAssembly, register_all_handlers
from kdive.jobs.capture_operations.supervisor import CaptureOperationSupervisor
from kdive.jobs.models import HandlerRegistry
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.infra.reaping import NullModuleVolumeReaper
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly
from tests.support.object_store import INERT_OBJECT_STORE


@pytest.mark.anyio
async def test_worker_routes_borrow_their_assembly_and_process_routes_remain_empty(monkeypatch):
    from kdive.assembly import ProcessAssembly
    from kdive.domain.catalog.resources import ResourceKind
    from kdive.jobs.assembly import build_worker_handler_assembly
    from kdive.jobs.models import ExternalBootAuthorityMarkerV1
    from kdive.providers.assembly import authority as authority_sender
    from kdive.providers.assembly import composition as providers
    from kdive.providers.core.resolver import ProviderBinding
    from kdive.providers.infra.reaping import NullModuleVolumeReaper
    from kdive.providers.remote_libvirt import composition as remote
    from kdive.providers.remote_libvirt.config import (
        RemoteAuthorityBinding,
        RemoteLibvirtConfig,
        TlsCertRefs,
    )
    from kdive.providers.system_authority.protocol import (
        AuthoritySystemMarkerV1,
        AuthoritySystemOperation,
    )
    from tests.jobs.handlers.external_boot.support import marker_fields

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
    reaper_factories = []

    def build_module_reaper(*, secret_registry, authority_sender_factory):
        del secret_registry
        reaper_factories.append(authority_sender_factory)
        return NullModuleVolumeReaper()

    monkeypatch.setattr(remote, "build_module_volume_reaper", build_module_reaper)
    owner = providers.ProviderComposition(
        secret_registry=SecretRegistry(), object_store=INERT_OBJECT_STORE
    )
    process = ProcessAssembly(ObjectStoreAssembly(INERT_OBJECT_STORE), owner)
    server_authority = (
        owner.build_provider_resolver()
        .resolve(ResourceKind.REMOTE_LIBVIRT)
        .for_resource("resource-a")
        .authority
    )
    assert server_authority is not None and server_authority.sender is None
    assert resolved == []
    workers = [
        build_worker_handler_assembly(
            process_assembly=process, incarnation_credential=SecretStr(value)
        )
        for value in ("worker-original-credential", "worker-replacement-credential")
    ]
    capabilities = [
        worker.resolver.resolve(ResourceKind.REMOTE_LIBVIRT).for_resource("resource-a").authority
        for worker in workers
    ]
    senders = []
    for capability in capabilities:
        assert capability is not None
        senders.append(capability.sender)
    assert all(sender is not None for sender in senders)
    marker = ExternalBootAuthorityMarkerV1.model_validate(
        marker_fields(provider_kind="remote-libvirt", authority_instance="authority-a")
    )
    for worker in workers:
        binding = ProviderBinding(
            ResourceKind.REMOTE_LIBVIRT,
            worker.resolver.resolve(ResourceKind.REMOTE_LIBVIRT).for_resource("resource-a"),
            "resource-a",
        )
        capability = binding.runtime.authority
        assert capability is not None
        assert worker.external_boot_client_factory is not None
        assert (
            worker.external_boot_client_factory(binding, marker, 123.0).sender is capability.sender
        )
        assert worker.authority_system_sender_factory is not None
        system_marker = AuthoritySystemMarkerV1(
            system_id=uuid4(),
            allocation_id=uuid4(),
            resource_id=uuid4(),
            provider_kind="remote-libvirt",
            resource_name="resource-a",
            authority_instance="authority-a",
            profile_identity="sha256:" + "a" * 64,
            root_identity="sha256:" + "b" * 64,
            operation=AuthoritySystemOperation.PROVISION,
            operation_identity="provision-a",
        )
        assert worker.authority_system_sender_factory(binding, system_marker) is capability.sender
        with pytest.raises(CategorizedError, match="binding-mismatch"):
            worker.authority_system_sender_factory(
                binding, system_marker.model_copy(update={"resource_name": "resource-b"})
            )
    assert resolved == []
    seen = []

    async def request(self, envelope, *, deadline):
        seen.append(json.loads(envelope)["credential"])
        return b'{"status":"ok","value":{"schema":"external-boot-authority-health-v1"}}'

    monkeypatch.setattr(authority_sender._AuthorityNetworkTransport, "_request_frame", request)
    for sender in senders:
        assert isinstance(sender, authority_sender.AuthorityRequestSender)
        await sender.health(deadline=asyncio.get_running_loop().time() + 1)
        assert all(callable(getattr(sender, slot)) for slot in sender.__slots__)
    reaper_senders = [factory(config.authority) for factory in reaper_factories]
    for sender in reaper_senders:
        await sender.health(deadline=asyncio.get_running_loop().time() + 1)
    assert resolved == [config.authority, config.authority, config.authority, config.authority]
    assert seen == [
        *(worker.incarnation_credential.get_secret_value() for worker in workers),
        *(worker.incarnation_credential.get_secret_value() for worker in workers),
    ]
    server_authority = (
        owner.build_provider_resolver()
        .resolve(ResourceKind.REMOTE_LIBVIRT)
        .for_resource("resource-a")
        .authority
    )
    assert server_authority is not None and server_authority.sender is None


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
        module_volume_reaper=NullModuleVolumeReaper(),
    )

    register_all_handlers(registry, assembly)

    registered = frozenset(kind for kind in JobKind if registry.get(kind) is not None)
    assert registered == ACTIVE_JOB_KINDS
    assert registered.isdisjoint(RETIRED_JOB_KINDS)


def test_authority_system_handler_receives_the_worker_artifact_store(monkeypatch) -> None:
    from kdive.jobs.handlers import systems

    seen = []

    def capture(*_args, authority_system, **_kwargs) -> None:
        seen.append(authority_system.artifact_store)

    monkeypatch.setattr(systems, "register_handlers", capture)
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
        module_volume_reaper=NullModuleVolumeReaper(),
    )

    register_all_handlers(HandlerRegistry(), assembly)

    assert seen == [INERT_OBJECT_STORE]
