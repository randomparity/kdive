"""Tests for provider runtime composition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.storage import StoredArtifact
from kdive.build_artifacts.results import BuildOutput
from kdive.components.references import (
    CONFIG_COMPONENT,
    PATCH_COMPONENT,
    LocalComponentRef,
)
from kdive.db.build_hosts import BuildHostKind
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.assembly import composition
from kdive.providers.core.discovery_registration import ProviderDiscoveryRegistration
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.infra.reaping import OwnedDomain
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.ports import (
    CaptureOutput,
    CrashOutput,
    InstallRequest,
    IntrospectOutput,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.remote_libvirt.retrieve.facade import RemoteLibvirtRetrieve
from kdive.providers.remote_libvirt.rootfs_build import RemoteLibvirtRootfsBuildPlane
from kdive.reconciler.console_telemetry import ConsoleTelemetry
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("22222222-2222-2222-2222-222222222222")

_REMOTE_INVENTORY = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
[[remote_libvirt]]
name = "host"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "clientcert.pem"
client_key_ref = "clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "cacert.pem"
base_image = "base"
cost_class = "remote"
vcpus = 8
memory_mb = 16384
"""


def _declare_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(_REMOTE_INVENTORY)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()


def _declare_no_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))


def _build_profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "file:///src/linux",
            "config": {"kind": "local", "path": "/configs/kdump.config"},
            "patch_ref": None,
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _provisioning_profile() -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 1,
            "memory_mb": 1024,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/x.qcow2"},
                }
            },
        }
    )


class _BuildProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def build(self, run_id: UUID, profile: ServerBuildProfile, **_: object) -> BuildOutput:
        assert isinstance(profile.config, LocalComponentRef)
        self.calls.append((run_id, profile.config.path))
        return BuildOutput(kernel_ref="k", debuginfo_ref="v", build_id="deadbeef")


class _ProvisionProvider:
    def provision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"

    def teardown(self, domain_name: str) -> None:
        self.torn_down = domain_name

    def reprovision(self, system_id: UUID, profile: object) -> str:
        return f"domain-{system_id}"


class _InstallProvider:
    def install(self, request: InstallRequest) -> None:
        self.installed = request

    def boot(self, system_id: UUID) -> None:
        self.booted = system_id


class _ConnectorProvider:
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        return TransportHandle(f"{kind}://{system}")

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed = handle


class _ControllerProvider:
    def power(self, domain_name: str, action: object) -> None:
        self.powered = (domain_name, action)

    def force_crash(self, domain_name: str) -> None:
        self.crashed = domain_name


class _RetrieveProvider:
    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        artifact = StoredArtifact("key", "etag", Sensitivity.SENSITIVE, "vmcore")
        return CaptureOutput(
            raw=artifact, redacted=artifact, vmcore_build_id="deadbeef", raw_size_bytes=0
        )

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        return CrashOutput(results={}, transcript="", truncated=False)


class _IntrospectorProvider:
    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)


def test_provider_runtime_returns_typed_provider_ports_directly() -> None:
    builder = _BuildProvider()
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    runtime = ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
        provisioner=_ProvisionProvider(),
        builder=builder,
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
    )

    output = runtime.builder.build(_RUN, _build_profile())

    assert output.build_id == "deadbeef"
    assert builder.calls == [(_RUN, "/configs/kdump.config")]
    assert runtime.installer is install
    assert runtime.booter is install


def test_default_runtime_advertises_implemented_component_sources_only() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.profile_policy, LocalLibvirtProfilePolicy)
    assert runtime.component_sources.provider == "local-libvirt"
    assert runtime.component_sources.accepted_component_sources == {
        "rootfs": frozenset({"catalog", "local"}),
        "kernel": frozenset({"local"}),
        "initrd": frozenset({"local"}),
        "config": frozenset({"catalog", "local"}),
        "patch": frozenset({"local"}),
        "vmlinux": frozenset({"local"}),
    }


def test_default_runtime_exposes_build_config_validator() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert runtime.build_config_validator is not None


def test_default_runtime_exposes_rootfs_build_plane() -> None:
    runtime = composition.build_local_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.rootfs_build_plane, LocalLibvirtRootfsBuildPlane)


def test_provider_runtime_discovery_hook_is_optional() -> None:
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    calls: list[AsyncConnectionPool] = []

    async def _register(pool: AsyncConnectionPool) -> None:
        calls.append(pool)

    runtime = ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
        provisioner=_ProvisionProvider(),
        builder=_BuildProvider(),
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
        discovery_registrar=_register,
    )
    pool = cast(AsyncConnectionPool, object())

    asyncio.run(runtime.register_discovery(pool))

    assert calls == [pool]


def test_provider_runtime_discovery_hook_noops_when_absent() -> None:
    install = _InstallProvider()
    retrieve = _RetrieveProvider()
    introspect = _IntrospectorProvider()
    runtime = ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
        provisioner=_ProvisionProvider(),
        builder=_BuildProvider(),
        installer=install,
        booter=install,
        connector=_ConnectorProvider(),
        controller=_ControllerProvider(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
    )

    asyncio.run(runtime.register_discovery(cast(AsyncConnectionPool, object())))


def test_default_resolver_registers_only_local_libvirt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)  # default = opt-in OFF
    _declare_no_remote(tmp_path, monkeypatch)
    resolver = composition.ProviderComposition().build_provider_resolver()
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})
    local = resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    assert local.component_sources.provider == "local-libvirt"


def test_enabling_fault_inject_registers_both_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.domain.catalog.resources import ResourceKind

    _declare_no_remote(tmp_path, monkeypatch)
    resolver = composition.ProviderComposition().build_provider_resolver(enable_fault_inject=True)

    assert resolver.registered_kinds() == frozenset(
        {ResourceKind.LOCAL_LIBVIRT, ResourceKind.FAULT_INJECT}
    )


def test_fault_inject_runtime_advertises_its_provider_identity() -> None:
    runtime = composition.build_fault_inject_runtime()

    assert isinstance(runtime.profile_policy, FaultInjectProfilePolicy)
    assert runtime.component_sources.provider == "fault-inject"
    assert runtime.discovery_registrar is not None


def test_fault_inject_runtime_provision_is_visible_to_a_reaper_on_the_same_inventory() -> None:
    import asyncio
    from uuid import UUID

    from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

    inventory = FaultInjectInventory()
    runtime = composition.build_fault_inject_runtime(inventory=inventory)
    system_id = UUID("33333333-3333-3333-3333-333333333333")

    domain = runtime.provisioner.provision(system_id, _provisioning_profile())

    # The shared-inventory seam: a domain the runtime provisions is reapable through a
    # FaultInjectReaper built over the same inventory (the reconciler leaked-domain seam).
    owned = asyncio.run(FaultInjectReaper(inventory).list_owned())
    assert [d.name for d in owned] == [domain]


@dataclass(frozen=True)
class _FakeOwnedDomain:
    """An OwnedDomain stand-in (structural: ``name`` + ``system_id``)."""

    name: str
    system_id: UUID | None = None


class _FakeLibvirtReaper:
    """A hermetic stand-in for the libvirt-backed reaper (no live connection in tests)."""

    def __init__(self, *owned: OwnedDomain) -> None:
        self._owned: list[OwnedDomain] = list(owned)
        self.destroyed: list[str] = []

    async def list_owned(self) -> list[OwnedDomain]:
        return list(self._owned)

    async def destroy(self, name: str) -> None:
        self.destroyed.append(name)


def test_reconciler_reaper_is_libvirt_backed_without_fault_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #372: a stock deployment's reaper is the libvirt-backed reaper (not NullReaper), so a
    # name-orphaned domain reaches repair_leaked_domains. (Previously asserted NullReaper —
    # that encoded the inert-predicate bug.)
    import asyncio

    monkeypatch.delenv("KDIVE_FAULT_INJECT", raising=False)
    owner = composition.ProviderComposition()

    sentinel = _FakeOwnedDomain(name="kdive-sentinel")
    reaper = owner.build_reconciler_reaper(libvirt_reaper=_FakeLibvirtReaper(sentinel))

    # No fault-inject → the single libvirt reaper is returned directly (not composed/Null).
    assert asyncio.run(reaper.list_owned()) == [sentinel]


def test_configured_fault_inject_runtime_is_visible_to_reconciler_reaper() -> None:
    import asyncio
    from uuid import UUID

    from kdive.domain.catalog.resources import ResourceKind

    owner = composition.ProviderComposition()
    resolver = owner.build_provider_resolver(enable_fault_inject=True)
    # Inject a hermetic libvirt reaper so the composite never opens a live qemu:/// connection.
    fake_libvirt = _FakeLibvirtReaper()
    reaper = owner.build_reconciler_reaper(enable_fault_inject=True, libvirt_reaper=fake_libvirt)
    system_id = UUID("44444444-4444-4444-4444-444444444444")

    domain = resolver.resolve(ResourceKind.FAULT_INJECT).provisioner.provision(
        system_id, _provisioning_profile()
    )

    # The composite unions the (empty) libvirt reaper rows with the fault-inject rows, so the
    # fault-inject domain is still visible and reapable.
    owned = asyncio.run(reaper.list_owned())
    assert domain in [item.name for item in owned]
    asyncio.run(reaper.destroy(domain))
    # The composite fans the *requested* name out to each member reaper verbatim, not a
    # placeholder.
    assert fake_libvirt.destroyed == [domain]


def test_reconciler_reaper_is_null_when_local_libvirt_disabled() -> None:
    # A deployment with no local libvirt (e.g. k8s, remote-libvirt only) opts the local reaper
    # out so repair_leaked_domains never tries to open a non-existent qemu:///system socket.
    import asyncio

    from kdive.providers.infra.reaping import NullReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_reaper(
        enable_local_libvirt=False,
        libvirt_reaper=_FakeLibvirtReaper(_FakeOwnedDomain(name="kdive-should-not-appear")),
    )

    assert isinstance(reaper, NullReaper)
    assert asyncio.run(reaper.list_owned()) == []


def test_reconciler_reaper_is_fault_inject_only_when_local_disabled() -> None:
    # Local disabled but fault-inject enabled: the reaper is the fault-inject one alone — the
    # injected libvirt sentinel must not surface (no composite with the opted-out local reaper).
    import asyncio

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_reaper(
        enable_local_libvirt=False,
        enable_fault_inject=True,
        libvirt_reaper=_FakeLibvirtReaper(_FakeOwnedDomain(name="kdive-should-not-appear")),
    )

    owned = [item.name for item in asyncio.run(reaper.list_owned())]
    assert "kdive-should-not-appear" not in owned


def test_local_libvirt_enabled_by_default_and_opt_out_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kdive.providers.assembly.composition import _local_libvirt_enabled

    monkeypatch.delenv("KDIVE_LOCAL_LIBVIRT_ENABLED", raising=False)
    config.reset()
    assert _local_libvirt_enabled(None) is True

    monkeypatch.setenv("KDIVE_LOCAL_LIBVIRT_ENABLED", "false")
    config.reset()
    assert _local_libvirt_enabled(None) is False

    # An explicit flag wins over the environment.
    assert _local_libvirt_enabled(True) is True


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "YES", " yes "])
def test_fault_inject_env_truthy_tokens_enable(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    # The env gate accepts 1/true/yes case-insensitively (whitespace stripped); each must
    # turn the default-off fault-inject opt-in on.
    from kdive.providers.assembly.composition import _fault_inject_enabled

    monkeypatch.setenv("KDIVE_FAULT_INJECT", truthy)
    config.reset()
    assert _fault_inject_enabled(None) is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "anything"])
def test_fault_inject_env_non_truthy_tokens_stay_disabled(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    from kdive.providers.assembly.composition import _fault_inject_enabled

    monkeypatch.setenv("KDIVE_FAULT_INJECT", falsy)
    config.reset()
    assert _fault_inject_enabled(None) is False


@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "NO", " no "])
def test_local_libvirt_env_falsy_tokens_disable(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    # local-libvirt is on by default; only the 0/false/no tokens (case-insensitive,
    # whitespace stripped) turn it off.
    from kdive.providers.assembly.composition import _local_libvirt_enabled

    monkeypatch.setenv("KDIVE_LOCAL_LIBVIRT_ENABLED", falsy)
    config.reset()
    assert _local_libvirt_enabled(None) is False


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "anything"])
def test_local_libvirt_env_other_tokens_stay_enabled(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    from kdive.providers.assembly.composition import _local_libvirt_enabled

    monkeypatch.setenv("KDIVE_LOCAL_LIBVIRT_ENABLED", truthy)
    config.reset()
    assert _local_libvirt_enabled(None) is True


def test_resolver_excludes_local_libvirt_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0131: disabling local-libvirt must drop its runtime from the resolver entirely, so
    # the reconciler's register_all_discovery never composes the local discovery registrar
    # (which would connect to a non-existent qemu:///system socket).
    _declare_remote(tmp_path, monkeypatch)
    resolver = composition.ProviderComposition().build_provider_resolver(enable_local_libvirt=False)

    assert ResourceKind.LOCAL_LIBVIRT not in resolver.registered_kinds()
    assert ResourceKind.REMOTE_LIBVIRT in resolver.registered_kinds()


def test_resolver_excludes_local_libvirt_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_LOCAL_LIBVIRT_ENABLED", "false")
    _declare_remote(tmp_path, monkeypatch)

    resolver = composition.ProviderComposition().build_provider_resolver()

    assert ResourceKind.LOCAL_LIBVIRT not in resolver.registered_kinds()


def test_disabled_local_libvirt_does_not_compose_local_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The local-libvirt discovery registration (creates=True) builds a LocalLibvirtDiscovery,
    # which opens the libvirt socket. With local disabled its runtime is not composed, so
    # register_all_discovery must never construct that target. (Remote discovery is bind-only,
    # creates=False, so it cannot be the failing sibling — see test_resolver.py for the
    # raise-isolation property over synthetic creates=True runtimes.)
    constructed: list[str] = []
    monkeypatch.setattr(
        "kdive.providers.local_libvirt.composition._discovery_target",
        lambda: constructed.append("local") or _unreachable_target(),
    )
    _declare_remote(tmp_path, monkeypatch)
    resolver = composition.ProviderComposition().build_provider_resolver(enable_local_libvirt=False)

    asyncio.run(resolver.register_all_discovery(cast(AsyncConnectionPool, object())))

    assert constructed == []


def _unreachable_target() -> object:
    raise AssertionError("local discovery target must not be constructed when local disabled")


def test_transport_resetter_is_null_without_remote() -> None:
    from kdive.providers.core.transport_reset import NullResetter

    comp = composition.ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=False)
    assert isinstance(resetter, NullResetter)


def test_transport_resetter_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter

    comp = composition.ProviderComposition()
    resetter = comp.build_reconciler_transport_resetter(enable_remote_libvirt=True)
    assert isinstance(resetter, RemoteLibvirtTransportResetter)


def test_dump_volume_reaper_is_null_without_remote() -> None:
    from kdive.providers.infra.reaping import NullDumpVolumeReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_dump_volume_reaper(enable_remote_libvirt=False)
    assert isinstance(reaper, NullDumpVolumeReaper)


def test_dump_volume_reaper_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.reaping.dump_volume import RemoteLibvirtDumpVolumeReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_dump_volume_reaper(enable_remote_libvirt=True)
    assert isinstance(reaper, RemoteLibvirtDumpVolumeReaper)


def test_build_vm_reaper_is_null_without_remote() -> None:
    from kdive.providers.infra.reaping import NullBuildVmReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_build_vm_reaper(enable_remote_libvirt=False)
    assert isinstance(reaper, NullBuildVmReaper)


def test_build_vm_reaper_is_remote_when_enabled() -> None:
    from kdive.providers.remote_libvirt.reaping.build_vm import RemoteLibvirtBuildVmReaper

    comp = composition.ProviderComposition()
    reaper = comp.build_reconciler_build_vm_reaper(enable_remote_libvirt=True)
    assert isinstance(reaper, RemoteLibvirtBuildVmReaper)


def test_console_hosting_is_none_without_remote() -> None:
    import asyncio

    comp = composition.ProviderComposition()

    assert asyncio.run(comp.build_reconciler_console_hosting(enable_remote_libvirt=False)) is None


def test_build_host_prober_is_wired_independent_of_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSH build-host prober is built unconditionally — not gated on remote-libvirt."""
    from kdive.providers.remote_libvirt import config as remote_config
    from kdive.providers.shared.build_host.reachability import BuildHostProber, SshBuildHostProber

    # Force remote-libvirt to read as unconfigured; the prober must still be returned.
    monkeypatch.setattr(remote_config, "is_remote_libvirt_configured", lambda: False)

    expected_registry = SecretRegistry()
    comp = composition.ProviderComposition(secret_registry=expected_registry)
    prober = comp.build_reconciler_build_host_prober()
    assert isinstance(prober, SshBuildHostProber)
    assert isinstance(prober, BuildHostProber)
    # The composition's shared registry is threaded into the prober (it redacts SSH
    # credential refs); a null registry would break that redaction.
    assert prober._secret_registry is expected_registry


def test_console_hosting_delegates_to_remote_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    expected_hosting = object()
    expected_registry = SecretRegistry()
    seen: dict[str, object] = {}

    async def _build_console_hosting(
        *,
        secret_registry: SecretRegistry,
        running_systems_factory: object,
        console_telemetry: object | None = None,
    ) -> object:
        seen["secret_registry"] = secret_registry
        seen["running_systems_factory"] = running_systems_factory
        seen["console_telemetry"] = console_telemetry
        return expected_hosting

    monkeypatch.setattr(
        composition.remote_composition, "build_console_hosting", _build_console_hosting
    )

    comp = composition.ProviderComposition(secret_registry=expected_registry)
    expected_telemetry = cast(ConsoleTelemetry, object())

    assert (
        asyncio.run(
            comp.build_reconciler_console_hosting(
                enable_remote_libvirt=True, console_telemetry=expected_telemetry
            )
        )
        is expected_hosting
    )
    assert seen["secret_registry"] is expected_registry
    assert seen["running_systems_factory"] is composition.DbRunningRemoteSystems
    # The caller's telemetry is threaded all the way through the factory, not dropped/Noned.
    assert seen["console_telemetry"] is expected_telemetry


def test_fault_inject_opt_in_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.domain.catalog.resources import ResourceKind

    monkeypatch.setenv("KDIVE_FAULT_INJECT", "1")

    resolver = composition.ProviderComposition().build_provider_resolver()

    assert ResourceKind.FAULT_INJECT in resolver.registered_kinds()


def test_fault_inject_runtime_without_engine_uses_bare_happy_path_ports() -> None:
    from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
    from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning

    runtime = composition.build_fault_inject_runtime()

    # No engine -> the happy-path ports are used unchanged (no faulting wrapper).
    assert isinstance(runtime.provisioner, FaultInjectProvisioning)
    assert isinstance(runtime.installer, FaultInjectInstall)
    assert isinstance(runtime.booter, FaultInjectInstall)


def test_fault_inject_runtime_with_engine_wraps_ports_in_faulting_decorators() -> None:
    from kdive.providers.fault_inject.faulting.engine import FaultEngine
    from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvisioning

    engine = FaultEngine(seed=7, fault_rate={"provision": 1.0}, max_latency_s={})
    runtime = composition.build_fault_inject_runtime(engine=engine)

    assert isinstance(runtime.provisioner, FaultedProvisioning)
    assert isinstance(runtime.installer, FaultedInstall)
    assert isinstance(runtime.booter, FaultedInstall)


def test_remote_libvirt_registers_via_inventory_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_remote(tmp_path, monkeypatch)

    resolver = composition.ProviderComposition().build_provider_resolver()

    assert ResourceKind.REMOTE_LIBVIRT in resolver.registered_kinds()


def test_remote_libvirt_explicit_flag_wins_over_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare_remote(tmp_path, monkeypatch)

    resolver = composition.ProviderComposition().build_provider_resolver(
        enable_remote_libvirt=False
    )

    assert ResourceKind.REMOTE_LIBVIRT not in resolver.registered_kinds()


def test_remote_libvirt_absent_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _declare_no_remote(tmp_path, monkeypatch)

    resolver = composition.ProviderComposition().build_provider_resolver()

    assert ResourceKind.REMOTE_LIBVIRT not in resolver.registered_kinds()


def test_remote_runtime_buildable_without_operator_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    # Buildability gates only construction (ADR-0076); config gates discovery/connection.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.discovery_registrar is not None


def test_remote_discovery_registration_is_bind_only() -> None:
    registration = composition.remote_composition.discovery_registration(
        secret_registry=SecretRegistry()
    )

    assert registration.kind is ResourceKind.REMOTE_LIBVIRT
    assert registration.creates is False


def test_build_host_transport_factories_follow_remote_libvirt_opt_in() -> None:
    provider_composition = composition.ProviderComposition(secret_registry=SecretRegistry())

    assert (
        provider_composition.build_build_host_transport_factories(enable_remote_libvirt=False) == {}
    )
    factories = provider_composition.build_build_host_transport_factories(
        enable_remote_libvirt=True
    )

    assert set(factories) == {BuildHostKind.EPHEMERAL_LIBVIRT}


def test_build_runtime_helpers_thread_registry_and_real_discovery_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The module-level build_*_runtime helpers must (a) pass the caller's registry to the
    # provider runtime builder and (b) attach the provider's *real* discovery registration
    # (not None) — a None registration crashes register_all_discovery at reconcile time.
    captured: list[object] = []
    real_with = composition._with_discovery_registration

    def _spy_with(
        runtime: ProviderRuntime, registration: ProviderDiscoveryRegistration
    ) -> ProviderRuntime:
        captured.append(registration)
        assert registration is not None
        return real_with(runtime, registration)

    monkeypatch.setattr(composition, "_with_discovery_registration", _spy_with)

    seen: dict[str, object] = {}
    real_local = composition.local_composition.build_runtime
    real_remote = composition.remote_composition.build_runtime

    def _local(*, secret_registry: SecretRegistry) -> ProviderRuntime:
        seen["local"] = secret_registry
        return real_local(secret_registry=secret_registry)

    def _remote(*, secret_registry: SecretRegistry) -> ProviderRuntime:
        seen["remote"] = secret_registry
        return real_remote(secret_registry=secret_registry)

    real_remote_disc = composition.remote_composition.discovery_registration

    def _remote_disc(*, secret_registry: SecretRegistry) -> object:
        seen["remote_disc"] = secret_registry
        return real_remote_disc(secret_registry=secret_registry)

    monkeypatch.setattr(composition.local_composition, "build_runtime", _local)
    monkeypatch.setattr(composition.remote_composition, "build_runtime", _remote)
    monkeypatch.setattr(composition.remote_composition, "discovery_registration", _remote_disc)

    local_registry = SecretRegistry()
    remote_registry = SecretRegistry()
    composition.build_local_runtime(secret_registry=local_registry)
    composition.build_fault_inject_runtime()
    composition.build_remote_runtime(secret_registry=remote_registry)

    assert seen["local"] is local_registry
    assert seen["remote"] is remote_registry
    assert seen["remote_disc"] is remote_registry
    # Each helper attached a real, non-None discovery registration (asserted in the spy).
    assert len(captured) == 3
    assert all(reg is not None for reg in captured)


def test_resolver_threads_shared_registry_into_provider_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resolver builds each provider runtime (and the remote discovery registration) with
    # the composition's shared secret registry; a null registry would disable redaction in
    # the constructed ports. Patch the underlying builders to capture the registry threaded.
    seen: dict[str, object] = {}
    real_local = composition.local_composition.build_runtime
    real_remote = composition.remote_composition.build_runtime
    real_remote_disc = composition.remote_composition.discovery_registration

    def _local(*, secret_registry: SecretRegistry) -> ProviderRuntime:
        seen["local"] = secret_registry
        return real_local(secret_registry=secret_registry)

    def _remote(*, secret_registry: SecretRegistry) -> ProviderRuntime:
        seen["remote"] = secret_registry
        return real_remote(secret_registry=secret_registry)

    def _remote_disc(*, secret_registry: SecretRegistry) -> object:
        seen["remote_disc"] = secret_registry
        return real_remote_disc(secret_registry=secret_registry)

    monkeypatch.setattr(composition.local_composition, "build_runtime", _local)
    monkeypatch.setattr(composition.remote_composition, "build_runtime", _remote)
    monkeypatch.setattr(composition.remote_composition, "discovery_registration", _remote_disc)

    _declare_remote(tmp_path, monkeypatch)
    expected_registry = SecretRegistry()
    composition.ProviderComposition(secret_registry=expected_registry).build_provider_resolver()

    assert seen["local"] is expected_registry
    assert seen["remote"] is expected_registry
    assert seen["remote_disc"] is expected_registry


def test_remote_factory_builders_thread_the_shared_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each remote-libvirt reconciler port is constructed with the composition's shared
    # secret registry (it drives credential redaction); a null registry would silently
    # disable that redaction. Patch the remote builders to capture the registry they receive.
    seen: dict[str, object] = {}

    def _capture(key: str, result: object):
        def builder(*, secret_registry: SecretRegistry) -> object:
            seen[key] = secret_registry
            return result

        return builder

    resetter_obj = object()
    dump_obj = object()
    build_vm_obj = object()
    transport_obj = object()
    monkeypatch.setattr(
        composition.remote_composition,
        "build_transport_resetter",
        _capture("resetter", resetter_obj),
    )
    monkeypatch.setattr(
        composition.remote_composition,
        "build_dump_volume_reaper",
        _capture("dump", dump_obj),
    )
    monkeypatch.setattr(
        composition.remote_composition,
        "build_build_vm_reaper",
        _capture("build_vm", build_vm_obj),
    )
    monkeypatch.setattr(
        composition.remote_composition,
        "build_ephemeral_build_transport_factory",
        _capture("transport", transport_obj),
    )

    expected_registry = SecretRegistry()
    comp = composition.ProviderComposition(secret_registry=expected_registry)

    assert comp.build_reconciler_transport_resetter(enable_remote_libvirt=True) is resetter_obj
    assert comp.build_reconciler_dump_volume_reaper(enable_remote_libvirt=True) is dump_obj
    assert comp.build_reconciler_build_vm_reaper(enable_remote_libvirt=True) is build_vm_obj
    factories = comp.build_build_host_transport_factories(enable_remote_libvirt=True)
    assert factories[BuildHostKind.EPHEMERAL_LIBVIRT] is transport_obj

    assert seen["resetter"] is expected_registry
    assert seen["dump"] is expected_registry
    assert seen["build_vm"] is expected_registry
    assert seen["transport"] is expected_registry


def test_remote_runtime_advertises_all_four_capture_methods() -> None:
    # M2.5 brings remote to 4/4 advertised methods: the two-phase kdump path (ADR-0084), the
    # host-side core-dump host_dump path (ADR-0094, #301), the already-wired gdbstub transport
    # (ADR-0083/0085, #302), and the reconciler-owned console collector (#303, ADR-0095).
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.supported_capture_methods == frozenset(
        {
            CaptureMethod.KDUMP,
            CaptureMethod.HOST_DUMP,
            CaptureMethod.GDBSTUB,
            CaptureMethod.CONSOLE,
        }
    )


def test_remote_runtime_advertises_host_dump_as_a_capture_method() -> None:
    # #301: HOST_DUMP is in vmcore.fetch's _VMCORE_METHODS, so advertising it admits
    # vmcore.fetch(method=host_dump) on remote through the existing tool.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.HOST_DUMP in runtime.supported_capture_methods


def test_remote_runtime_advertises_gdbstub_as_a_capture_method() -> None:
    # AC2: GDBSTUB is counted by the advertised capability surface. gdbstub is not
    # consumed through vmcore.fetch (only HOST_DUMP/KDUMP are), so there is no selection
    # path to gate; the assertion is membership in the advertised set.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.GDBSTUB in runtime.supported_capture_methods


def test_remote_runtime_advertises_console_as_a_capture_method() -> None:
    # #303 (ADR-0095): CONSOLE is in the advertised set so the reconciler-owned collector's
    # artifact is selectable. Like gdbstub, console is consumed off the boot/diagnostic plane,
    # not through vmcore.fetch, so the assertion is membership in the advertised set.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert CaptureMethod.CONSOLE in runtime.supported_capture_methods


def test_remote_runtime_gdbstub_debug_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC2 no-regression: advertising GDBSTUB does not alter the existing connect/attach
    # debug path (ADR-0083/0085) — the remote attach seam and connector are unchanged.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
    from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.debug is not None
    assert runtime.debug.attach_seam is remote_attach_seam
    assert isinstance(runtime.connector, RemoteLibvirtConnect)


def test_remote_runtime_has_real_control_and_retrieve() -> None:
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.profile_policy, RemoteLibvirtProfilePolicy)
    assert isinstance(runtime.controller, RemoteLibvirtControl)
    assert isinstance(runtime.retriever, RemoteLibvirtRetrieve)
    assert runtime.crash_postmortem is runtime.retriever


def test_remote_runtime_has_real_provisioner(monkeypatch: pytest.MonkeyPatch) -> None:
    # The provisioning plane is real from this issue on; it must construct without
    # any operator config (config is read per op, ADR-0076/0080).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.provisioner, RemoteLibvirtProvisioning)


def test_remote_runtime_has_noop_rootfs_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    # The systems registrar hard-fails on rootfs_validator=None, so the remote runtime
    # must supply the no-op contract (a remote profile has no rootfs; it is never
    # invoked) - the fault-inject precedent.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.rootfs_validator is not None


def test_remote_runtime_has_real_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    # The remote Build plane is real from this issue on (ADR-0081); it must construct
    # without operator config (the build env is read per op).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.builder, RemoteLibvirtBuild)


def test_remote_runtime_exposes_rootfs_build_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.rootfs_build_plane, RemoteLibvirtRootfsBuildPlane)


def test_remote_runtime_has_real_installer_and_booter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The remote Install/Boot plane is real from this issue on (ADR-0082); it must construct
    # without operator config, and one object realizes both ports (as local does).
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.installer, RemoteLibvirtInstall)
    assert runtime.booter is runtime.installer


def test_remote_runtime_wires_connect_and_introspect_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The connect/debug + introspection planes are real (ADR-0083); control/retrieve are
    # real from issue #206 on (ADR-0084), asserted in test_remote_runtime_has_real_control_*.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
    from kdive.providers.remote_libvirt.debug.introspect import (
        RemoteLibvirtLiveIntrospect,
        RemoteLibvirtVmcoreIntrospect,
    )
    from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.connector, RemoteLibvirtConnect)
    assert runtime.debug is not None
    assert runtime.debug.attach_seam is remote_attach_seam
    assert isinstance(runtime.vmcore_introspector, RemoteLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, RemoteLibvirtLiveIntrospect)


def test_remote_runtime_wires_build_config_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    # runs.build runs the config validator after the component-source gate; without it a
    # remote build's config ref goes unvalidated. It must be the builder's validator.
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)

    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    assert runtime.build_config_validator is not None


def test_remote_runtime_accepts_local_and_catalog_config_and_local_patch_sources() -> None:
    # runs.build rejects a config whose source-kind is not advertised; an empty set rejects
    # every remote build. The remote server build merges a kdump fragment from a local .config
    # or the seeded catalog entry + applies an optional local patch, so it advertises CONFIG as
    # {"catalog", "local"} and PATCH as {"local"} (ADR-0081/0096).
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())

    accepted = runtime.component_sources.accepted_component_sources
    assert accepted.get(CONFIG_COMPONENT) == frozenset({"catalog", "local"})
    assert accepted.get(PATCH_COMPONENT) == frozenset({"local"})
    assert runtime.component_sources.provider == ResourceKind.REMOTE_LIBVIRT.value
