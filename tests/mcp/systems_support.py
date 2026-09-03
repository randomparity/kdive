"""Shared support helpers for systems-tool tests and cross-family scenarios."""

from __future__ import annotations

import base64
import copy
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.components.references import ComponentKind
from kdive.components.validation import ComponentSourceCapabilities
from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS, SYSTEMS
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import SystemPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle.systems.admin import SystemAdminHandlers
from kdive.mcp.tools.lifecycle.systems.provision import SystemProvisionHandlers
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.core.runtime import (
    BootstrapKeyCapabilities,
    ConsoleCapabilities,
    ProviderRuntime,
    ProviderSupport,
    RootfsCapabilities,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    authorized_key_customizer,
)
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.ports.lifecycle import (
    DEBUG_TRANSPORT_KINDS,
    INTROSPECTION_MODES,
    DebugTransportKind,
    IntrospectionMode,
)
from kdive.providers.ports.traffic import TrafficCaptureOperationPorts
from kdive.security.authz.rbac import Role
from kdive.serialization import JsonValue
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

TEST_DT = datetime(2026, 1, 1, tzinfo=UTC)
TEST_PROFILE_POLICY = LocalLibvirtProfilePolicy()


class _ResolvingLiveIntrospector:
    """A benign live-introspector default whose ADR-0335 runtime probe always resolves.

    The default drgn-live attach and introspect tests should not trip the runtime resolution probe,
    so ``run_script`` returns instead of raising ``DEBUG_ATTACH_FAILURE``; a test that exercises the
    probe passes its own ``live_introspector`` to :func:`provider_resolver`.
    """

    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float, key_path: str
    ) -> None:
        del transport_handle, script, timeout_sec, key_path


class _NoopSnapshotter:
    """Snapshot-capable test port whose lifecycle methods intentionally do nothing."""

    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None:
        del domain_name, name, include_memory

    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
        del domain_name, name, start_paused

    def delete(self, domain_name: str, name: str) -> None:
        del domain_name, name

    def delete_all(self, domain_name: str) -> None:
        del domain_name


# ROOTFS only, matching every real provider since ADR-0563 (#1942): it is the one kind the
# provision path passes to `reject_unsupported_component_source`. A CONFIG entry here modelled a
# declaration nothing read.
TEST_COMPONENT_SOURCES = ComponentSourceCapabilities(
    provider="test-provider",
    accepted_component_sources={
        ComponentKind.ROOTFS: frozenset({"catalog", "local"}),
    },
)
SYSTEM_PROVISION_HANDLERS = SystemProvisionHandlers(
    TEST_PROFILE_POLICY,
    TEST_COMPONENT_SOURCES,
    lambda _: None,
)
SYSTEM_ADMIN_HANDLERS = SystemAdminHandlers(
    TEST_PROFILE_POLICY,
    TEST_COMPONENT_SOURCES,
    lambda _: None,
)

PROVISIONING_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def provisioning_profile() -> dict[str, Any]:
    return copy.deepcopy(PROVISIONING_PROFILE)


def fault_inject_profile() -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"] = {"fault-inject": {"capture_method": "host_dump"}}
    return profile


def upload_profile() -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["rootfs"] = {
        "kind": "upload",
        # A valid base64 SHA-256 (32 zero bytes); the content address the fetch resolves against.
        "checksum_sha256": base64.b64encode(bytes(32)).decode("ascii"),
    }
    return profile


def provider_resolver(
    *,
    provisioner: object | None = None,
    installer: object | None = None,
    booter: object | None = None,
    connector: object | None = None,
    controller: object | None = None,
    retriever: object | None = None,
    crash_postmortem: object | None = None,
    vmcore_introspector: object | None = None,
    live_introspector: object | None = None,
    supported_capture_methods: frozenset[CaptureMethod] | None = None,
    supported_debug_transports: frozenset[DebugTransportKind] | None = None,
    supported_introspection: frozenset[IntrospectionMode] | None = None,
    profile_policy: object | None = None,
    platform_root_cmdline: str | None = "root=/dev/vda",
    bootstrap_key_customizer: Callable[[str], Callable[[str], None]] | None = (
        authorized_key_customizer
    ),
    snapshotter: object | None = None,
    supports_snapshots: bool = True,
    traffic_capturer: object | None = None,
    supports_traffic_capture: bool = True,
    supports_diagnostic_sysrq: bool = True,
    supports_crash_watch: bool = True,
    console_reader: object | None = None,
    external_boot: object | None = None,
) -> ProviderResolver:
    """Return a local-libvirt resolver with optional fake runtime ports.

    ``platform_root_cmdline`` defaults to the local-libvirt root device; pass ``None`` to model a
    provider (e.g. remote-libvirt) whose in-guest bootloader owns the root device (ADR-0183).

    The support descriptor fields default to the **full** set so a test provider is capable by
    default (matching the historic permissive capture-method default); a
    capability-aware-admission test (ADR-0209) passes an empty/narrowed set to model an unsupported
    plane. ``vmcore_introspector`` is injectable so an admission *admit*-path test can run a fake
    port behind the gate; it defaults to an unused port for the deny/short-circuit tests.
    ``bootstrap_key_customizer`` (ADR-0289, #963) defaults to the real local-libvirt injector,
    matching production composition; pass ``None`` to model a provider with no local overlay.

    ``external_boot`` (ADR-0583/0584) binds an :class:`ExternalBootPorts` implementation under the
    **local-libvirt** kind. That pairing is deliberate: the fault-inject composition registers its
    runtime under ``ResourceKind.FAULT_INJECT``, a value ``ExternalBootAuthorityMarkerV1``'s
    ``provider_kind`` cannot hold and ``allocate_external_boot_authority`` rejects, so binding the
    fault-inject *port* here is what makes it usable without the fault-inject *kind*.
    """
    unused_port = cast(Any, object())
    runtime = ProviderRuntime(
        profile_policy=cast(
            Any, profile_policy if profile_policy is not None else TEST_PROFILE_POLICY
        ),
        provisioner=cast(Any, provisioner if provisioner is not None else unused_port),
        installer=cast(Any, installer if installer is not None else unused_port),
        booter=cast(Any, booter if booter is not None else unused_port),
        connector=cast(Any, connector if connector is not None else unused_port),
        controller=cast(Any, controller if controller is not None else unused_port),
        retriever=cast(Any, retriever if retriever is not None else unused_port),
        crash_postmortem=cast(
            Any, crash_postmortem if crash_postmortem is not None else unused_port
        ),
        vmcore_introspector=cast(
            Any, vmcore_introspector if vmcore_introspector is not None else unused_port
        ),
        live_introspector=cast(
            Any,
            live_introspector if live_introspector is not None else _ResolvingLiveIntrospector(),
        ),
        support=ProviderSupport(
            component_sources=TEST_COMPONENT_SOURCES,
            capture_methods=(
                supported_capture_methods
                if supported_capture_methods is not None
                else frozenset(CaptureMethod)
            ),
            debug_transports=(
                supported_debug_transports
                if supported_debug_transports is not None
                else DEBUG_TRANSPORT_KINDS
            ),
            introspection=(
                supported_introspection
                if supported_introspection is not None
                else INTROSPECTION_MODES
            ),
            supports_snapshots=supports_snapshots,
            supports_traffic_capture=supports_traffic_capture,
            supports_diagnostic_sysrq=supports_diagnostic_sysrq,
            supports_crash_watch=supports_crash_watch,
        ),
        rootfs=RootfsCapabilities(validator=lambda _: None),
        platform_root_cmdline=platform_root_cmdline,
        bootstrap_key=(
            None
            if bootstrap_key_customizer is None
            else BootstrapKeyCapabilities(customizer=bootstrap_key_customizer)
        ),
        snapshot=cast(Any, snapshotter if snapshotter is not None else _NoopSnapshotter())
        if supports_snapshots
        else None,
        traffic_capturer=cast(
            Any, traffic_capturer if traffic_capturer is not None else unused_port
        )
        if supports_traffic_capture
        else None,
        traffic_capture_operation=(
            TrafficCaptureOperationPorts(
                configuration=lambda _resource_id: b"test-configuration",
                quiescence=lambda _raw: unused_port,
            )
            if supports_traffic_capture
            else None
        ),
        # ``console_reader`` models a provider (remote-libvirt) whose console is read through the
        # ADR-0429 strict read seam rather than a worker-local file; the control handlers pick the
        # remote path when ``console.reader_factory`` is set (ADR-0433, #1435).
        console=(
            None
            if console_reader is None
            else ConsoleCapabilities(
                snapshotter=unused_port, reader_factory=lambda: cast(Any, console_reader)
            )
        ),
        external_boot=cast(Any, external_boot) if external_boot is not None else None,
    )
    return ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})


def ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    conn_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await conn_pool.open()
    try:
        yield conn_pool
    finally:
        await conn_pool.close()


async def granted_allocation(
    conn_pool: AsyncConnectionPool,
    *,
    cap: int = 2,
    systems_quota: int = 1_000_000,
    requested_vcpus: int | None = None,
    requested_memory_gb: int | None = None,
    requested_disk_gb: int | None = None,
    shape: str | None = None,
) -> str:
    """Seed a granted Allocation; pass the ``requested_*``/``shape`` snapshot for a shape-sized
    allocation, or leave them ``None`` for the no-snapshot (full-custom/legacy) lane."""
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with conn_pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=1_000_000,
                max_concurrent_systems=systems_quota,
                updated_at=TEST_DT,
            ),
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=TEST_DT,
            ),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                resource_id=res.id,
                state=AllocationState.GRANTED,
                requested_vcpus=requested_vcpus,
                requested_memory_gb=requested_memory_gb,
                requested_disk_gb=requested_disk_gb,
                shape=shape,
            ),
        )
    return str(alloc.id)


async def seed_system(
    conn_pool: AsyncConnectionPool,
    allocation_id: str,
    state: SystemState,
    *,
    resolved_cpu: dict[str, JsonValue] | None = None,
) -> str:
    """Insert a System for an existing Allocation and return its id."""
    async with conn_pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=TEST_DT,
                updated_at=TEST_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(allocation_id),
                state=state,
                provisioning_profile=provisioning_profile(),
                resolved_cpu=resolved_cpu,
            ),
        )
    return str(system.id)


class FakeProvisioning:
    """Records provision/teardown/reprovision calls; provision returns a name or raises."""

    def __init__(self, *, provision_error: bool = False, reprovision_error: bool = False) -> None:
        self.provisioned: list[UUID] = []
        self.torn_down: list[str] = []
        self.reprovisioned: list[UUID] = []
        self.overlay_customizers: list[tuple[Any, ...]] = []
        self.bootstrap_pubkeys: list[str | None] = []
        self._provision_error = provision_error
        self._reprovision_error = reprovision_error

    def provision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        self.provisioned.append(system_id)
        self.overlay_customizers.append(overlay_customizers)
        self.bootstrap_pubkeys.append(bootstrap_pubkey)
        if self._provision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, Any] | None:
        del system_id
        return self.resolved_cpu

    resolved_cpu: dict[str, Any] | None = None

    def reprovision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        self.reprovisioned.append(system_id)
        self.overlay_customizers.append(overlay_customizers)
        self.bootstrap_pubkeys.append(bootstrap_pubkey)
        if self._reprovision_error:
            raise CategorizedError("boom", category=ErrorCategory.PROVISIONING_FAILURE)
        return f"kdive-{system_id}"


async def enqueue_provision(conn_pool: AsyncConnectionPool, system_id: str, alloc_id: str) -> Job:
    async with conn_pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.PROVISION,
            SystemPayload(system_id=system_id),
            {"principal": "user-1", "agent_session": "s", "project": "proj"},
            f"{alloc_id}:provision",
        )
