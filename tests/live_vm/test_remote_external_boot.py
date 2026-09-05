"""Native remote-libvirt external-boot and recovery proof (#2121)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import cast
from uuid import uuid4

import libvirt
import pytest

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    BundleSource,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    InitrdSource,
    MaterializedArtifacts,
    ModuleObligation,
    OpaqueProviderRef,
    PlanOwnership,
    RootSource,
    RootSpecV1,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    OBSERVATION_PROGRAMS,
    RemoteExternalBootRecovery,
    activate_definition,
    observe_guest_identity,
    prepare_target_definition,
    recover_disk_grub_baseline,
)
from kdive.providers.remote_libvirt.lifecycle.port_allocation import allocate_port, used_gdb_ports
from kdive.providers.remote_libvirt.lifecycle.readiness import ReadinessConn, wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    BootArtifactVolumeConn,
    artifact_volume_name,
    materialize_boot_artifacts,
)
from kdive.providers.remote_libvirt.lifecycle.storage import OverlayPool, ensure_overlay
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name, render_domain_xml
from kdive.providers.remote_libvirt.reaping.boot_artifacts import (
    BootArtifactReaperConn,
    reap_orphaned_boot_artifacts,
)
from kdive.providers.shared.guest_agent import AgentExecResult, GuestAgentExec, qemu_agent_command
from tests.live_vm import require_live_vm_remote
from tests.live_vm.test_remote_external_boot_support import (
    assert_cmdline_equal,
    attempt_all_cleanup,
    first_differing_byte,
    preserved_components,
    read_cmdline_early,
    read_kernel_identity,
    sha256_digest,
)


@pytest.mark.parametrize(
    ("expected", "observed", "offset"),
    [
        (b"a", b"b", 0),
        (b"abc", b"axc", 1),
        (b"abc", b"abcd", 3),
        (b"abcd", b"abc", 3),
    ],
)
def test_cmdline_mismatch_names_both_values_and_first_offset(
    expected: bytes, observed: bytes, offset: int
) -> None:
    assert first_differing_byte(expected, observed) == offset
    with pytest.raises(AssertionError) as caught:
        assert_cmdline_equal(expected, observed)
    message = str(caught.value)
    assert f"byte offset {offset}" in message
    assert f"expected={expected!r}" in message
    assert f"observed={observed!r}" in message


def test_cmdline_equality_passes() -> None:
    assert_cmdline_equal(b"root=/dev/vda1", b"root=/dev/vda1")


class _Agent:
    def __init__(self, result: AgentExecResult) -> None:
        self.result = result

    def run(
        self, domain: object, argv: list[str], *, input_data: str | None = None
    ) -> AgentExecResult:
        del domain, argv, input_data
        return self.result


class _Domain:
    def name(self) -> str:
        return "test-domain"


def test_early_cmdline_read_strips_exactly_one_newline() -> None:
    agent = _Agent(AgentExecResult(0, b"root=/dev/vda1\n\n", b""))
    assert read_cmdline_early(agent, _Domain()) == b"root=/dev/vda1\n"


@pytest.mark.parametrize(
    "result",
    (AgentExecResult(1, b"", b"failed"), AgentExecResult(0, b"truncated", b"")),
)
def test_early_cmdline_read_rejects_failure_or_truncation(result: AgentExecResult) -> None:
    with pytest.raises(AssertionError):
        read_cmdline_early(_Agent(result), _Domain())


def test_cleanup_attempts_every_action() -> None:
    calls: list[str] = []

    def action(name: str, *, fail: bool = False) -> Callable[[], None]:
        def run() -> None:
            calls.append(name)
            if fail:
                raise RuntimeError(name)

        return run

    with pytest.raises(ExceptionGroup) as caught:
        attempt_all_cleanup(
            [
                ("first", action("first", fail=True)),
                ("second", action("second")),
                ("third", action("third", fail=True)),
            ]
        )
    assert calls == ["first", "second", "third"]
    assert [str(error) for error in caught.value.exceptions] == ["first: first", "third: third"]


def test_cleanup_preserves_primary_failure() -> None:
    primary = RuntimeError("carrier")

    with pytest.raises(ExceptionGroup) as caught:
        attempt_all_cleanup(
            [("cleanup", lambda: (_ for _ in ()).throw(RuntimeError("cleanup")))],
            primary=primary,
        )

    assert caught.value.exceptions[0] is primary
    assert "cleanup: cleanup" in str(caught.value.exceptions[1])


def _profile(base_image: str) -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "provider": {
                "remote-libvirt": {"base_image_volume": base_image, "crashkernel": "256M"}
            },
        }
    )


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_external_boot_survives_worker_loss_and_recovers() -> None:
    """Prove native remote external boot, early cmdline identity, recovery, and teardown."""
    contract = require_live_vm_remote()
    system_id, run_id, generation, activation_id = (uuid4() for _ in range(4))
    domain_name = f"kdive-{system_id}"
    artifact_pool_name = f"kdive-live-{run_id}"
    artifact_pool_path = f"/var/lib/libvirt/images/{artifact_pool_name}"
    cmdline = f"root={contract.root_device} console=ttyS0"
    conn = None
    domain = None
    domain_defined = False
    overlay_created = False
    artifact_pool_defined = False
    primary: Exception | None = None
    try:
        conn = libvirt.open(contract.libvirt_uri)
        pool = conn.storagePoolLookupByName("default")
        artifact_pool = conn.storagePoolDefineXML(
            f"<pool type='dir'><name>{artifact_pool_name}</name>"
            f"<target><path>{artifact_pool_path}</path></target></pool>"
        )
        artifact_pool_defined = True
        artifact_pool.build(0)
        artifact_pool.create(0)
        overlay = ensure_overlay(cast("OverlayPool", pool), contract.base_image, system_id)
        overlay_created = True
        gdb_port = allocate_port(
            used_gdb_ports(conn), own_name=domain_name, port_min=47001, port_max=47099
        )
        xml = render_domain_xml(
            system_id,
            _profile(contract.base_image),
            pool="default",
            volume=overlay_volume_name(system_id),
            overlay_path=overlay.path,
            backing_path=overlay.backing_path,
            gdb_addr=contract.gdb_addr,
            gdb_port=gdb_port,
        )
        domain = conn.defineXML(xml)
        domain_defined = True
        domain.create()
        wait_for_agent(
            cast("ReadinessConn", conn),
            domain_name,
            monotonic=time.monotonic,
            sleep=time.sleep,
            timeout_s=180,
            poll_s=2,
        )
        agent = GuestAgentExec(
            agent_command=qemu_agent_command, allowed_programs=OBSERVATION_PROGRAMS
        )
        kernel_identity = read_kernel_identity(agent, domain)
        baseline = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        before = preserved_components(baseline)
        domain.destroy()

        kernel_bytes = contract.kernel.read_bytes()
        initrd_bytes = contract.initrd.read_bytes()
        artifacts = materialize_boot_artifacts(
            cast("BootArtifactVolumeConn", conn),
            artifact_pool_name,
            system_id=system_id,
            run_id=run_id,
            kernel=kernel_bytes,
            initrd=initrd_bytes,
        )
        digest = sha256_digest(kernel_bytes)
        initrd_digest = sha256_digest(initrd_bytes)
        ownership = PlanOwnership(
            system_id=str(system_id), run_id=str(run_id), build_generation=str(generation)
        )
        plan = ExternalBootPlan(
            architecture="x86_64",
            ownership=ownership,
            bundle=BundleSource(
                key="live/kernel",
                version="one-shot",
                sha256=digest,
                vmlinuz_sha256=digest,
                member_count=1,
                uncompressed_bytes=len(kernel_bytes),
                vmlinuz_size_bytes=len(kernel_bytes),
                decoded_kernel_size_bytes=len(kernel_bytes),
                elf_metadata_bytes=1,
                gnu_build_id_size_bytes=len(kernel_identity.gnu_build_id) // 2,
            ),
            initrd=InitrdSource(
                key="live/initrd",
                version="one-shot",
                sha256=initrd_digest,
                size_bytes=len(initrd_bytes),
            ),
            cmdline=cmdline,
            debug_cmdline=None,
            platform_arguments=(f"root={contract.root_device}", "console=ttyS0"),
            module_obligation=ModuleObligation(
                release=kernel_identity.release,
                source_manifest=digest,
                member_count=1,
                uncompressed_bytes=1,
            ),
            root=RootSpecV1(
                architecture="x86_64",
                root=contract.root_device,
                arguments=(f"root={contract.root_device}",),
                authority="stage-inspection",
                source=RootSource(kind="staged-image", identity=digest),
            ),
        )
        materialization = ExternalBootMaterialization(
            architecture="x86_64",
            provider_kind="remote-libvirt",
            ownership=ActivationOwnership(system_id=str(system_id), run_id=str(run_id)),
            plan_identity=plan.identity,
            extracted_vmlinuz_sha256=digest,
            source_module_manifest=digest,
            installed_module_tree=digest,
            verified_bundle_sha256=digest,
            verified_initrd_sha256=initrd_digest,
            kernel_observation=kernel_identity,
            artifacts=MaterializedArtifacts(
                kernel=artifacts.kernel,
                modules=OpaqueProviderRef(ref="live/modules"),
                initrd=artifacts.initrd,
            ),
        )
        binding = ExternalBootActivationBinding(
            system_id=str(system_id), run_id=str(run_id), activation_id=str(activation_id)
        )
        definition = prepare_target_definition(
            baseline,
            plan=plan,
            materialization=materialization,
            binding=binding,
            pool="default",
            overlay_path=overlay.path,
            kernel_path=artifact_pool.storageVolLookupByName(
                artifact_volume_name("kernel", system_id, run_id, digest)
            ).path(),
            initrd_path=artifact_pool.storageVolLookupByName(
                artifact_volume_name("initrd", system_id, run_id, initrd_digest)
            ).path(),
        )
        activate_definition(conn, definition)
        wait_for_agent(
            conn,
            domain_name,
            monotonic=time.monotonic,
            sleep=time.sleep,
            timeout_s=180,
            poll_s=2,
        )
        domain = conn.lookupByName(domain_name)
        observed_cmdline = read_cmdline_early(agent, domain)
        assert_cmdline_equal(cmdline.encode(), observed_cmdline)
        observation = observe_guest_identity(agent, domain, definition)
        assert observation.cmdline == observed_cmdline
        assert preserved_components(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)) == before

        conn.close()
        conn = None
        conn = libvirt.open(contract.libvirt_uri)
        recovery = RemoteExternalBootRecovery(definition=definition, prior_power="running")
        recover_disk_grub_baseline(conn, recovery)
        wait_for_agent(
            conn,
            domain_name,
            monotonic=time.monotonic,
            sleep=time.sleep,
            timeout_s=180,
            poll_s=2,
        )
        domain = conn.lookupByName(domain_name)
        assert read_kernel_identity(agent, domain) == kernel_identity
        recover_disk_grub_baseline(conn, recovery)
        wait_for_agent(
            conn,
            domain_name,
            monotonic=time.monotonic,
            sleep=time.sleep,
            timeout_s=180,
            poll_s=2,
        )
        domain = conn.lookupByName(domain_name)
        assert read_kernel_identity(agent, domain) == kernel_identity
        assert (
            reap_orphaned_boot_artifacts(
                cast("BootArtifactReaperConn", conn), artifact_pool_name, live_owners=()
            )
            == 2
        )
    except Exception as exc:
        primary = exc
    finally:

        def cleanup_conn() -> libvirt.virConnect:
            nonlocal conn
            if conn is None:
                conn = libvirt.open(contract.libvirt_uri)
            return conn

        def cleanup_domain() -> None:
            cleanup_domain = cleanup_conn().lookupByName(domain_name)
            if cleanup_domain.isActive():
                cleanup_domain.destroy()
            cleanup_domain.undefine()

        def cleanup_overlay() -> None:
            cleanup_conn().storagePoolLookupByName("default").storageVolLookupByName(
                overlay_volume_name(system_id)
            ).delete(0)

        def cleanup_artifact_pool() -> None:
            cleanup_pool = cleanup_conn().storagePoolLookupByName(artifact_pool_name)
            for volume in cleanup_pool.listAllVolumes(0):
                volume.delete(0)
            if cleanup_pool.isActive():
                cleanup_pool.destroy()
            cleanup_pool.delete(0)
            cleanup_pool.undefine()

        def close_connection() -> None:
            assert conn is not None
            conn.close()

        actions: list[tuple[str, Callable[[], None]]] = []
        if domain_defined:
            actions.append(("domain", cleanup_domain))
        if overlay_created:
            actions.append(("overlay", cleanup_overlay))
        if artifact_pool_defined:
            actions.append(("artifact pool", cleanup_artifact_pool))
        if conn is not None:
            actions.append(("connection", close_connection))
        attempt_all_cleanup(actions, primary=primary)
