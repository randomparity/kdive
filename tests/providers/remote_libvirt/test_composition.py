"""Remote-libvirt runtime composition assertions (ADR-0183, ADR-0187)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.components.references import (
    CONFIG_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.components.validation import reject_unsupported_component_source
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.runtime import DebugCapabilities
from kdive.providers.remote_libvirt import composition
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLibvirtLiveIntrospect,
    RemoteLibvirtVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import (
    RemoteLibvirtProvisioning,
)
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.remote_libvirt.retrieve.postmortem import CrashPostmortemAdapter
from kdive.providers.remote_libvirt.retrieve.retriever import RemoteLibvirtRetriever
from kdive.providers.remote_libvirt.rootfs_build import RemoteLibvirtRootfsBuildPlane
from kdive.security.secrets.secret_registry import SecretRegistry


def test_remote_runtime_owns_no_platform_root_cmdline() -> None:
    # The remote base image is partitioned and boots via in-guest GRUB (root=UUID, inherited by
    # grubby --copy-default). The platform must not inject a root device or it overrides that — so
    # the remote runtime carries platform_root_cmdline=None, unlike local's "root=/dev/vda" (#587).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.platform_root_cmdline is None


def test_remote_runtime_advertises_and_wires_snapshots() -> None:
    # ADR-0428 (#1430): remote sets supports_snapshots=True *and* wires the Snapshotter port. The
    # two must agree (the #1428 parity guard enforces the pairing); a snapshot-capable provider
    # sets both, so systems.snapshot/restore/list/delete are reachable against a remote System.
    from kdive.providers.remote_libvirt.lifecycle.snapshot import RemoteLibvirtSnapshotter

    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.support.supports_snapshots is True
    assert isinstance(runtime.snapshot, RemoteLibvirtSnapshotter)


def test_remote_runtime_advertises_and_wires_traffic_capture() -> None:
    # ADR-0432 (#1434): remote sets supports_traffic_capture=True *and* wires the TrafficCapturer
    # port. The two must agree (the #1428 parity guard enforces the pairing); a capture-capable
    # provider sets both, so control.capture_traffic is reachable against a remote System.
    from kdive.providers.remote_libvirt.lifecycle.traffic_capture import (
        RemoteLibvirtTrafficCapture,
    )

    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.support.supports_traffic_capture is True
    assert isinstance(runtime.traffic_capturer, RemoteLibvirtTrafficCapture)


def test_remote_runtime_sets_rebind_for_resource() -> None:
    # ADR-0187: the remote runtime carries a per-resource rebind hook so the resolver can bind it
    # to the granted host; the base runtime is buildable without operator config (ADR-0076).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.binding is not None
    assert runtime.binding.rebind_for_resource is not None


def test_rebind_for_resource_threads_resource_name_into_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0187: for_resource('host-b') must resolve the provisioner's config for host-b — proving
    # the resource name is threaded through build_runtime into the port's config_factory.
    seen: list[str] = []

    def fake_for_resource(name: str) -> RemoteLibvirtConfig:
        seen.append(name)
        return RemoteLibvirtConfig(
            uri=f"qemu+tls://{name}.example/system",
            cert_refs=TlsCertRefs(
                client_cert_ref="c", client_key_ref="k", ca_cert_ref="ca"
            ),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
            gdb_addr="10.0.0.1",
        )

    monkeypatch.setattr(composition, "remote_config_for_resource", fake_for_resource)
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)
    bound = runtime.for_resource("host-b")
    # The provisioner resolves its connection config lazily; pull it to trigger the factory.
    cfg = bound.provisioner._connections.config()  # ty: ignore[unresolved-attribute]
    assert cfg.uri == "qemu+tls://host-b.example/system"
    assert seen == ["host-b"]
    # The rebind must also thread the original secret_registry into the rebound runtime, not None.
    assert bound.installer._secret_registry is registry  # ty: ignore[unresolved-attribute]


def test_for_resource_is_identity_without_rebind_hook() -> None:
    # A runtime with no rebind hook (single-host providers like local-libvirt) returns itself.
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    plain = replace(runtime, binding=None)
    assert plain.for_resource("anything") is plain


def test_console_open_resolves_the_systems_own_host_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0187: per-System console open must resolve the host config of the resource the System is
    # allocated to (System→Allocation→Resource.name), so one leader hosts a multi-host fleet.
    system_id = uuid4()
    secret_backend = object()
    resolved: list[str] = []
    opened: list[tuple[RemoteLibvirtConfig, object]] = []

    def fake_name_lookup(conninfo: str, sid: object) -> str:
        assert sid == system_id
        assert conninfo == "postgresql://ignored"
        return "host-b"

    def fake_for_resource(name: str) -> RemoteLibvirtConfig:
        resolved.append(name)
        return RemoteLibvirtConfig(
            uri=f"qemu+tls://{name}.example/system",
            cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
            gdb_addr="10.0.0.2",
        )

    def fake_open_remote_console(config: RemoteLibvirtConfig, backend: object, sid: object):  # noqa: ANN202
        assert backend is secret_backend
        opened.append((config, sid))
        return object()

    monkeypatch.setattr(composition, "resource_name_for_system", fake_name_lookup)
    monkeypatch.setattr(composition, "remote_config_for_resource", fake_for_resource)
    monkeypatch.setattr(composition, "open_remote_console", fake_open_remote_console)

    composition._open_console_for_system(
        system_id,
        conninfo="postgresql://ignored",
        secret_backend=secret_backend,  # ty: ignore[invalid-argument-type]
    )

    assert resolved == ["host-b"]
    assert opened[0][0].uri == "qemu+tls://host-b.example/system"
    assert opened[0][1] == system_id


def test_build_runtime_wires_each_port_to_its_remote_adapter() -> None:
    # build_runtime must wire every port to its remote-libvirt adapter (not None and not a
    # different adapter). booter reuses the installer instance; capture and crash postmortem use
    # separate ports so capture-method dispatch cannot hide crash-command behavior.
    runtime = composition.build_runtime(secret_registry=SecretRegistry())

    assert isinstance(runtime.profile_policy, RemoteLibvirtProfilePolicy)
    assert isinstance(runtime.provisioner, RemoteLibvirtProvisioning)
    assert isinstance(runtime.installer, RemoteLibvirtInstall)
    assert runtime.booter is runtime.installer
    assert isinstance(runtime.connector, RemoteLibvirtConnect)
    assert isinstance(runtime.controller, RemoteLibvirtControl)
    assert isinstance(runtime.retriever, RemoteLibvirtRetriever)
    assert isinstance(runtime.crash_postmortem, CrashPostmortemAdapter)
    assert runtime.crash_postmortem is not runtime.retriever
    assert isinstance(runtime.vmcore_introspector, RemoteLibvirtVmcoreIntrospect)
    assert isinstance(runtime.live_introspector, RemoteLibvirtLiveIntrospect)
    assert runtime.rootfs is not None
    assert isinstance(runtime.rootfs.build_plane, RemoteLibvirtRootfsBuildPlane)


def test_build_runtime_supported_capture_methods() -> None:
    # The remote provider supports exactly KDUMP, HOST_DUMP, GDBSTUB, CONSOLE.
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.support.capture_methods == frozenset(
        {
            CaptureMethod.KDUMP,
            CaptureMethod.HOST_DUMP,
            CaptureMethod.GDBSTUB,
            CaptureMethod.CONSOLE,
        }
    )


def test_build_runtime_debug_capabilities_are_wired() -> None:
    # debug must be a populated DebugCapabilities (attach_seam + engine), not None.
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert isinstance(runtime.debug, DebugCapabilities)
    assert runtime.debug.attach_seam is composition.remote_attach_seam
    assert runtime.debug.engine is not None
    # The remote engine must use the ACL-remote host policy (not the loopback default).
    assert (
        runtime.debug.engine._host_policy is composition.allow_acl_remote  # ty: ignore[unresolved-attribute]
    )


def test_build_runtime_engine_redactor_uses_the_provider_secret_registry() -> None:
    # The gdb/MI engine's redactor factory must be seeded from the provider's secret_registry so
    # secrets registered there are redacted from debug output (not a fresh/empty registry).
    registry = SecretRegistry()
    registry.register("s3cr3t-token", scope=None)  # pragma: allowlist secret
    runtime = composition.build_runtime(secret_registry=registry)

    redactor = runtime.debug.engine._redactor_factory()  # ty: ignore[unresolved-attribute]
    assert redactor is not None
    assert "s3cr3t-token" not in redactor.redact_text("value=s3cr3t-token tail")


def test_build_runtime_validators_and_component_sources() -> None:
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    # Remote owns rootfs image builds, but has no provider-specific rootfs validation to add.
    assert runtime.rootfs is not None
    assert runtime.rootfs.validator is None
    # component_sources reflects _component_sources(): the remote provider id and source map.
    sources = runtime.support.component_sources
    assert sources.provider == ResourceKind.REMOTE_LIBVIRT.value
    assert sources.accepted_component_sources[CONFIG_COMPONENT] == frozenset({"catalog", "local"})
    assert sources.accepted_component_sources[PATCH_COMPONENT] == frozenset({"local"})
    # ADR-0430 (#1432): remote now accepts a supplied kernel + vmlinux from the worker-host `local`
    # source kind, matching local-libvirt.
    assert sources.accepted_component_sources[KERNEL_COMPONENT] == frozenset({"local"})
    assert sources.accepted_component_sources[VMLINUX_COMPONENT] == frozenset({"local"})


def test_component_sources_map_directly() -> None:
    # Exercise the module-level builder directly so its exact contents are pinned.
    caps = composition._component_sources()
    assert caps.provider == ResourceKind.REMOTE_LIBVIRT.value
    assert caps.accepted_component_sources == {
        # ADR-0440 (#1433): a supplied ROOTFS base image from the worker-host `local` source kind;
        # NOT `catalog` (that role is the operator-staged base_image_volume) and NOT `upload`.
        ROOTFS_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }


def test_remote_accepts_supplied_kernel_and_vmlinux_from_local_source() -> None:
    # ADR-0430 (#1432): a worker-host-local supplied KERNEL/VMLINUX is accepted (parity with local);
    # the reject-path helper returns without raising for the `local` source kind.
    caps = composition._component_sources()
    local_ref = LocalComponentRef(kind="local", path="/var/lib/kdive/kernel/vmlinuz-6.9")
    for kind in (KERNEL_COMPONENT, VMLINUX_COMPONENT):
        reject_unsupported_component_source(caps, component_kind=kind, ref=local_ref)


def test_remote_accepts_supplied_rootfs_from_local_source() -> None:
    # ADR-0440 (#1433): a worker-host-local supplied ROOTFS base image is accepted; the reject-path
    # helper returns without raising for the `local` source kind.
    caps = composition._component_sources()
    assert caps.accepted_component_sources[ROOTFS_COMPONENT] == frozenset({"local"})
    local_ref = LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/fedora-44.qcow2")
    reject_unsupported_component_source(caps, component_kind=ROOTFS_COMPONENT, ref=local_ref)


def test_remote_rejects_catalog_and_upload_rootfs_sources() -> None:
    # ADR-0440 (#1433): ROOTFS accepts only `local`. A `catalog` ref (the operator-staged
    # base_image_volume's role) is rejected; component-upload/artifact stay unaccepted too.
    caps = composition._component_sources()
    catalog_ref = CatalogComponentRef(kind="catalog", provider="remote-libvirt", name="fedora-44")
    artifact_ref = ArtifactComponentRef(kind="artifact", artifact_id=uuid4())
    for ref, kind_name in ((catalog_ref, "catalog"), (artifact_ref, "artifact")):
        with pytest.raises(CategorizedError) as caught:
            reject_unsupported_component_source(caps, component_kind=ROOTFS_COMPONENT, ref=ref)
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details["source_kind"] == kind_name
        assert caught.value.details["accepted_source_kinds"] == ["local"]


def test_remote_rejects_component_upload_and_artifact_kernel_vmlinux_sources() -> None:
    # component-upload stays unaccepted for every kind (agent upload is a non-goal, #1423); an
    # artifact source is likewise not advertised. Both keep the existing configuration-error shape.
    caps = composition._component_sources()
    artifact_ref = ArtifactComponentRef(kind="artifact", artifact_id=uuid4())
    for kind in (KERNEL_COMPONENT, VMLINUX_COMPONENT):
        with pytest.raises(CategorizedError) as caught:
            reject_unsupported_component_source(caps, component_kind=kind, ref=artifact_ref)
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details["source_kind"] == "artifact"
        assert caught.value.details["accepted_source_kinds"] == ["local"]


def test_build_runtime_threads_secret_registry_into_each_registry_port() -> None:
    # Ports that own a secret_registry must receive the provider's instance, not None / a stand-in.
    registry = SecretRegistry()
    runtime = composition.build_runtime(secret_registry=registry)

    assert runtime.installer._secret_registry is registry  # ty: ignore[unresolved-attribute]
    assert runtime.live_introspector._secret_registry is registry  # ty: ignore[unresolved-attribute]
    assert runtime.vmcore_introspector._secret_registry is registry  # ty: ignore[unresolved-attribute]
    # Retriever capture collaborators and the separate crash-postmortem port all receive the same
    # provider registry.
    assert runtime.retriever._kdump._secret_registry is registry  # ty: ignore[unresolved-attribute]
    assert runtime.retriever._host_dump._secret_registry is registry  # ty: ignore[unresolved-attribute]
    assert runtime.crash_postmortem._secret_registry is registry  # ty: ignore[unresolved-attribute]
    # controller and provisioner consume the registry lazily via a secret-backend factory
    # closure; the built backend must carry the provider registry, not None.
    assert runtime.controller._secret_backend_factory()._registry is registry  # ty: ignore[unresolved-attribute]
    assert (
        runtime.provisioner._connections.secret_backend_factory()._registry  # ty: ignore[unresolved-attribute]
        is registry
    )


def test_build_runtime_threads_config_factory_into_each_config_port() -> None:
    # Ports that resolve a per-host RemoteLibvirtConfig must receive the runtime's config_factory
    # (ADR-0187), so each resolves to the same config the factory yields — not None / the default.
    sentinel = RemoteLibvirtConfig(
        uri="qemu+tls://host-a.example/system",
        cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
        concurrent_allocation_cap=1,
        gdb_addr="10.0.0.9",
    )
    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), config_factory=lambda: sentinel
    )

    assert runtime.installer._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.controller._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.connector._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.live_introspector._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.retriever._kdump._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.retriever._host_dump._config_factory() is sentinel  # ty: ignore[unresolved-attribute]
    assert runtime.provisioner._connections.config() is sentinel  # ty: ignore[unresolved-attribute]


def test_build_runtime_staged_volume_probe_threads_config_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staged_volume_probe must call probe_staged_volumes with the volumes positionally and the
    # runtime's config_factory threaded through (ADR-0187 per-host config).
    captured: dict[str, object] = {}

    def fake_config_factory() -> RemoteLibvirtConfig:
        return RemoteLibvirtConfig(
            uri="qemu+tls://host-a.example/system",
            cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
            gdb_addr="10.0.0.3",
        )

    def fake_probe(volumes: object, *, config_factory: object) -> str:
        captured["volumes"] = volumes
        captured["config_factory"] = config_factory
        return "sentinel"

    monkeypatch.setattr(composition, "probe_staged_volumes", fake_probe)
    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), config_factory=fake_config_factory
    )
    assert runtime.resource_details is not None
    assert runtime.resource_details.staged_volume_probe is not None
    result = runtime.resource_details.staged_volume_probe(["vol-1", "vol-2"])

    assert result == "sentinel"
    assert captured["volumes"] == ["vol-1", "vol-2"]
    assert captured["config_factory"] is fake_config_factory


def test_build_runtime_resource_detail_projector_threads_config_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_config_factory() -> RemoteLibvirtConfig:
        return RemoteLibvirtConfig(
            uri="qemu+tls://host-a.example/system",
            cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
            gdb_addr="10.0.0.3",
        )

    def fake_probe(volumes: object, *, config_factory: object) -> str:
        captured["volumes"] = volumes
        captured["config_factory"] = config_factory
        return "sentinel"

    async def fake_project(
        pool: object, viewer_projects: object, *, staged_probe: object
    ) -> dict[str, object]:
        captured["pool"] = pool
        captured["viewer_projects"] = viewer_projects
        captured["probe_result"] = staged_probe(["vol-1"])  # ty: ignore[call-non-callable]
        return {"staged_base_images": []}

    monkeypatch.setattr(composition, "probe_staged_volumes", fake_probe)
    monkeypatch.setattr(composition, "project_resource_details", fake_project)
    runtime = composition.build_runtime(
        secret_registry=SecretRegistry(), config_factory=fake_config_factory
    )
    assert runtime.resource_details is not None
    projector = runtime.resource_details.projector
    assert projector is not None
    result = asyncio.run(projector(cast(Any, "pool"), ("proj",)))

    assert result == {"staged_base_images": []}
    assert captured["pool"] == "pool"
    assert captured["viewer_projects"] == ("proj",)
    assert captured["probe_result"] == "sentinel"
    assert captured["volumes"] == ["vol-1"]
    assert captured["config_factory"] is fake_config_factory
