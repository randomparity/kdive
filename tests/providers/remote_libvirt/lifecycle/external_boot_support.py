"""Shared remote external-boot plan, materialization, and domain fixtures."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    BundleSource,
    ExternalBootMaterialization,
    ExternalBootPlan,
    InitrdSource,
    KernelIdentity,
    MaterializedArtifacts,
    ModuleObligation,
    OpaqueProviderRef,
    PlanOwnership,
    RootSource,
    RootSpecV1,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name, render_domain_xml
from kdive.providers.shared.guest_agent import AgentExecResult

_SYSTEM_ID = UUID("00000000-0000-0000-0000-00000000beef")
_RUN_ID = "00000000-0000-0000-0000-000000000001"
_BUILD_GENERATION = "00000000-0000-0000-0000-000000000b01"
_SHA = "sha256:" + "11" * 32
_INITRD_SHA = "sha256:" + "22" * 32
_MANIFEST = "sha256:" + "33" * 32
_TREE = "sha256:" + "44" * 32
_ROOT_IDENTITY = "sha256:" + "55" * 32
_NOTES = bytes.fromhex("040000000800000003000000474e5500") + bytes.fromhex("ab" * 8)
_CMDLINE = b"root=/dev/vda1 console=ttyS0\n"


class _FakeAgentExec:
    """Answers an exact argv with a canned result; an unconfigured argv is a hard failure."""

    def __init__(self, replies: dict[tuple[str, ...], AgentExecResult]) -> None:
        self._replies = replies
        self.argvs: list[list[str]] = []
        self.error: BaseException | None = None

    def run(
        self, domain: Any, argv: list[str], *, input_data: str | None = None
    ) -> AgentExecResult:
        del domain, input_data
        self.argvs.append(list(argv))
        if self.error is not None:
            raise self.error
        key = tuple(argv)
        if key not in self._replies:
            raise AssertionError(f"unconfigured argv: {argv}")
        return self._replies[key]


def _replies(
    *,
    release: bytes = b"6.9.0-kdive\n",
    machine: bytes = b"x86_64\n",
    cmdline: bytes = _CMDLINE,
    notes: bytes = _NOTES,
    exits: dict[str, int] | None = None,
) -> dict[tuple[str, ...], AgentExecResult]:
    codes = exits or {}
    return {
        ("/usr/bin/uname", "-r"): AgentExecResult(codes.get("release", 0), release, b""),
        ("/usr/bin/uname", "-m"): AgentExecResult(codes.get("machine", 0), machine, b""),
        ("/usr/bin/cat", "/proc/cmdline"): AgentExecResult(codes.get("cmdline", 0), cmdline, b""),
        ("/usr/bin/cat", "/sys/kernel/notes"): AgentExecResult(codes.get("notes", 0), notes, b""),
    }


def _remote_profile(**section_overrides: Any) -> ProvisioningProfile:
    section: dict[str, Any] = {
        "base_image_volume": "kdive-base-fedora-42.qcow2",
        "crashkernel": "256M",
        **section_overrides,
    }
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {"remote-libvirt": section},
        }
    )


def _source_xml(
    *, system_id: UUID = _SYSTEM_ID, pool: str = "kdive", volume: str | None = None
) -> str:
    """The provisioned disk/GRUB baseline a remote System actually carries."""
    return render_domain_xml(
        system_id,
        _remote_profile(),
        pool=pool,
        volume=volume if volume is not None else overlay_volume_name(system_id),
        overlay_path="/pool/overlay.qcow2",
        backing_path="/pool/base.qcow2",
        gdb_addr="10.0.0.5",
        gdb_port=1234,
        ssh_addr="10.0.0.5",
        ssh_port=2222,
    )


def _kernel_identity() -> KernelIdentity:
    return KernelIdentity(architecture="x86_64", release="6.9.0-kdive", gnu_build_id="ab" * 8)


def _plan(*, with_initrd: bool = True) -> ExternalBootPlan:
    arguments = ("root=/dev/vda1", "console=ttyS0")
    return ExternalBootPlan(
        schema="external-boot-plan-v1",
        architecture="x86_64",
        ownership=PlanOwnership(
            system_id=str(_SYSTEM_ID), run_id=_RUN_ID, build_generation=_BUILD_GENERATION
        ),
        bundle=BundleSource(
            key="builds/kernel.tar",
            version="v1",
            sha256=_SHA,
            vmlinuz_sha256=_SHA,
            member_count=12,
            uncompressed_bytes=4096,
            vmlinuz_size_bytes=2048,
            decoded_kernel_size_bytes=4096,
            elf_metadata_bytes=512,
            gnu_build_id_size_bytes=8,
        ),
        initrd=(
            InitrdSource(key="builds/initrd.img", version="v1", sha256=_INITRD_SHA, size_bytes=1024)
            if with_initrd
            else None
        ),
        cmdline="root=/dev/vda1 console=ttyS0",
        debug_cmdline=None,
        platform_arguments=arguments,
        module_obligation=ModuleObligation(
            release="6.9.0-kdive", source_manifest=_MANIFEST, member_count=3, uncompressed_bytes=64
        ),
        root=RootSpecV1(
            schema="root-spec-v1",
            architecture="x86_64",
            root="/dev/vda1",
            arguments=("root=/dev/vda1",),
            authority="stage-inspection",
            source=RootSource(kind="staged-image", identity=_ROOT_IDENTITY),
        ),
    )


def _materialization(
    *, plan: ExternalBootPlan | None = None, with_initrd: bool = True, run_id: str = _RUN_ID
) -> ExternalBootMaterialization:
    plan = plan if plan is not None else _plan(with_initrd=with_initrd)
    return ExternalBootMaterialization(
        schema="external-boot-materialization-v1",
        architecture="x86_64",
        provider_kind="remote-libvirt",
        ownership=ActivationOwnership(system_id=str(_SYSTEM_ID), run_id=run_id),
        plan_identity=plan.identity,
        extracted_vmlinuz_sha256=_SHA,
        source_module_manifest=_MANIFEST,
        installed_module_tree=_TREE,
        verified_bundle_sha256=_SHA,
        verified_initrd_sha256=_INITRD_SHA if with_initrd else None,
        kernel_observation=_kernel_identity(),
        artifacts=MaterializedArtifacts(
            kernel=OpaqueProviderRef(ref="kernel/abc"),
            modules=OpaqueProviderRef(ref="modules/abc"),
            initrd=OpaqueProviderRef(ref="initrd/abc") if with_initrd else None,
        ),
    )
