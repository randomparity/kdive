"""Unit tests for the in-process local-libvirt rootfs build plane (M2.4/2, ADR-0092, ADR-0251).

These cover the plane's orchestration and provenance contract without libguestfs, qemu, or the
network: every slow/external seam (``acquire_base``, the ``virt-customize`` runner, the repack, and
the family's ``normalize``) is an injected stub the tests record. The real libguestfs path is
exercised on the operator-run live-stack path. The plane now resolves the catalog row for
``spec.name`` (falling back to a virt-builder template for an uncataloged old-style spec) and
drives ``acquire base → virt-customize(family argv) → repack ext4 → family.normalize → output``.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families.base import CustomizeContext, FamilyCustomizer
from kdive.images.families.renderers import render_argv
from kdive.images.families.rhel import RhelFamily
from kdive.images.families.steps import (
    InstallPackages,
    RunCommand,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.images.planes._build_common import (
    BootEntriesProbeSeam,
    DrgnProbeSeam,
    KernelConfigProbeSeam,
    MakedumpfileProbeSeam,
    OsReleaseProbeSeam,
    VersionInspectSeam,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.rootfs.catalog import (
    CloudImageSource,
    RootfsSource,
    VirtBuilderSource,
    resolve_rootfs_entry,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import BaselineKernel
from kdive.providers.local_libvirt.rootfs_build import (
    LocalLibvirtRootfsBuildPlane,
    RootfsBuildTools,
    _parse_os_release,
    family_for,
)


def _spec(**overrides: object) -> RootfsBuildSpec:
    base: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "fedora-kdive-ready-43",
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("openssh-server", "drgn"),
        "source_image_digest": "ignored:caller-declared",
        "capabilities": ("agent", "kdump", "drgn"),
    }
    base.update(overrides)
    return RootfsBuildSpec(**base)  # ty: ignore[invalid-argument-type]


@dataclass
class _FakeFamily:
    """A FamilyCustomizer stub recording its calls into the shared recorder."""

    rec: _Recorder
    family: str = "rhel"
    kdump_unit: str = "kdump.service"
    guest_mac: str = "selinux-permissive"
    customize_via: str = "virt_customize"

    def packages(self, kind: str, distro: str, version: str) -> tuple[str, ...]:
        return ("marker-pkg",)

    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        return (Capability.SSH,)

    def customize_steps(self, ctx: CustomizeContext) -> list[Step]:
        self.rec.customize_ctxs.append(ctx)
        # The plane renders the kdive-ready unit (with this family's kdump_unit) before calling us
        # and unlinks it afterwards, so capture its content here to guard the point-6 wiring (#824).
        self.rec.readiness_unit_texts.append(ctx.readiness_unit_path.read_text())
        return [InstallPackages(("marker-pkg",)), RunCommand("marker-customize")]

    def normalize(self, qcow2: Path, *, relabel: bool = True) -> None:
        self.rec.order.append("normalize")
        self.rec.normalize_calls.append(qcow2)


@dataclass
class _Recorder:
    """Stub seams that record the staged build operations in call order."""

    order: list[str] = field(default_factory=list)
    acquired_sources: list[RootfsSource] = field(default_factory=list)
    customize_argvs: list[list[str]] = field(default_factory=list)
    customize_ctxs: list[CustomizeContext] = field(default_factory=list)
    repack_calls: list[tuple[Path, Path]] = field(default_factory=list)
    repack_sizes: list[str] = field(default_factory=list)
    normalize_calls: list[Path] = field(default_factory=list)
    readiness_unit_texts: list[str] = field(default_factory=list)
    verify_calls: list[Path] = field(default_factory=list)
    family_kdump_unit: str = "kdump.service"
    payload: bytes = b"qcow2-bytes"

    def acquire_base(
        self,
        source: RootfsSource,
        scratch: Path,
        *,
        releasever: str,
        arch: str,
        virt_builder: object,
        downloader: object,
    ) -> None:
        scratch.write_bytes(b"scratch")
        self.order.append("acquire")
        self.acquired_sources.append(source)

    def customize(self, qcow2: Path, argv: list[str]) -> None:
        self.order.append("customize")
        self.customize_argvs.append(argv)

    def repack_whole_disk_ext4(self, *, scratch: Path, qcow2: Path, size: str) -> None:
        qcow2.write_bytes(self.payload)
        self.order.append("repack")
        self.repack_calls.append((scratch, qcow2))
        self.repack_sizes.append(size)

    def family_for(self, name: str) -> FamilyCustomizer:
        return _FakeFamily(self, kdump_unit=self.family_kdump_unit)

    def verify_cloud_init(self, qcow2: Path) -> None:
        self.order.append("verify")
        self.verify_calls.append(qcow2)


def _no_versions(_qcow2: Path) -> dict[str, str]:
    return {}


def _no_makedumpfile(_qcow2: Path) -> str | None:
    return None


def _no_drgn(_qcow2: Path) -> str | None:
    return None


def _no_boot_entries(_qcow2: Path) -> list[str] | None:
    # Hermetic default: no listing, so the default build path omits boot_kernel_count and never
    # shells out to the real guestfish probe (ADR-0295).
    return None


def _no_os_release(_qcow2: Path) -> str | None:
    # Hermetic default: no os-release text, so the default build path omits os_release (ADR-0311).
    return None


def _no_kernel_config(_qcow2: Path, _version: str) -> bytes | None:
    # Hermetic default: no config bytes, so the default build path offers no config (ADR-0317).
    return None


def _tools(
    rec: _Recorder,
    inspect_versions: VersionInspectSeam = _no_versions,
    probe_makedumpfile: MakedumpfileProbeSeam = _no_makedumpfile,
    verify_cloud_init: object | None = None,
    probe_boot_entries: BootEntriesProbeSeam = _no_boot_entries,
    probe_os_release: OsReleaseProbeSeam = _no_os_release,
    probe_kernel_config: KernelConfigProbeSeam = _no_kernel_config,
    probe_drgn: DrgnProbeSeam = _no_drgn,
) -> RootfsBuildTools:
    return RootfsBuildTools(
        acquire_base=rec.acquire_base,
        customize=rec.customize,
        repack_whole_disk_ext4=rec.repack_whole_disk_ext4,
        family_for=rec.family_for,
        inspect_versions=inspect_versions,
        probe_makedumpfile=probe_makedumpfile,
        probe_drgn=probe_drgn,
        probe_boot_entries=probe_boot_entries,
        probe_os_release=probe_os_release,
        probe_kernel_config=probe_kernel_config,
        verify_cloud_init=verify_cloud_init or rec.verify_cloud_init,  # ty: ignore[invalid-argument-type]
    )


def _plane(
    tmp_path: Path,
    rec: _Recorder,
    inspect_versions: VersionInspectSeam = _no_versions,
    probe_makedumpfile: MakedumpfileProbeSeam = _no_makedumpfile,
    probe_boot_entries: BootEntriesProbeSeam = _no_boot_entries,
    probe_os_release: OsReleaseProbeSeam = _no_os_release,
    probe_kernel_config: KernelConfigProbeSeam = _no_kernel_config,
    probe_drgn: DrgnProbeSeam = _no_drgn,
) -> LocalLibvirtRootfsBuildPlane:
    return LocalLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=_tools(
            rec,
            inspect_versions,
            probe_makedumpfile,
            probe_boot_entries=probe_boot_entries,
            probe_os_release=probe_os_release,
            probe_kernel_config=probe_kernel_config,
            probe_drgn=probe_drgn,
        ),
    )


def test_default_workspace_is_the_managed_build_path() -> None:
    # With no workspace override the plane defaults to the managed images path, not a
    # null/derived location; from_env carries that same default through.
    assert LocalLibvirtRootfsBuildPlane()._workspace == Path("/var/lib/kdive/build/images")
    assert LocalLibvirtRootfsBuildPlane.from_env()._workspace == Path("/var/lib/kdive/build/images")


def test_build_produces_qcow2_with_content_digest(tmp_path: Path) -> None:
    rec = _Recorder(payload=b"the-image-bytes")
    out = _plane(tmp_path, rec).build(_spec())

    assert isinstance(out, RootfsBuildOutput)
    assert out.qcow2_path.exists()
    assert out.qcow2_path.read_bytes() == b"the-image-bytes"
    expected = "sha256:" + hashlib.sha256(b"the-image-bytes").hexdigest()
    assert out.digest == expected, "image identity is the qcow2 content digest"


def test_build_drives_acquire_customize_repack_normalize_in_order(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())

    assert rec.order == ["acquire", "customize", "repack", "normalize", "verify"], (
        "pipeline is acquire base → virt-customize → repack ext4 → family.normalize → "
        "verify_cloud_init"
    )
    # The family customizer (not a hardcoded SELinux edit) builds the virt-customize argv.
    assert rec.customize_argvs == [["--install", "marker-pkg", "--run-command", "marker-customize"]]
    assert len(rec.repack_calls) == 1
    scratch_path, staged_qcow2 = rec.repack_calls[0]
    assert scratch_path.name == "scratch.qcow2", "the acquired scratch image is customized in place"
    assert rec.repack_sizes == ["6G"], "the configured size flows to the repack stage"
    assert rec.normalize_calls == [staged_qcow2], "the repacked image is normalized before publish"
    assert out.qcow2_path.name == staged_qcow2.name


def test_build_runs_cloud_init_self_check_after_normalize(tmp_path: Path) -> None:
    # The plane must run verify_cloud_init on the staged image, after normalize, before publish.
    rec = _Recorder()
    _plane(tmp_path, rec).build(_spec())
    assert rec.verify_calls, "verify_cloud_init must run on the built image"
    assert rec.order.index("verify") > rec.order.index("normalize")


def test_build_fails_when_cloud_init_self_check_rejects(tmp_path: Path) -> None:
    rec = _Recorder()

    def _reject(_qcow2: Path) -> None:
        raise CategorizedError(
            "cloud-init self-check failed",
            category=ErrorCategory.PROVISIONING_FAILURE,
        )

    plane = LocalLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=_tools(rec, verify_cloud_init=_reject),
    )
    with pytest.raises(CategorizedError) as err:
        plane.build(_spec())
    assert err.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_customize_context_threads_cloud_image_flag(tmp_path: Path) -> None:
    rec = _Recorder()
    # fedora-kdive-ready-44 is a cloud-image catalog row; the flag must reach the customizer.
    _plane(tmp_path, rec).build(_spec(name="fedora-kdive-ready-44"))
    ctx = rec.customize_ctxs[0]
    assert ctx.is_cloud_image is True
    assert ctx.readiness_unit_path.suffix == ".service"

    rec2 = _Recorder()
    _plane(tmp_path, rec2).build(_spec(name="fedora-kdive-ready-43"))
    assert rec2.customize_ctxs[0].is_cloud_image is False, "a virt-builder row is not a cloud image"


def test_readiness_unit_is_rendered_with_the_family_kdump_unit(tmp_path: Path) -> None:
    # The plane (not the family) renders the kdive-ready unit; it must order After= the family's
    # kdump_unit so a non-rhel family closes the arm-vs-ready race (ADR-0251 point 6 / #824). This
    # guards the wiring (plane -> family.kdump_unit), which the readiness_unit() unit test alone
    # cannot: a revert to a hardcoded unit would still pass that test.
    rec = _Recorder(family_kdump_unit="kdump-tools.service")
    _plane(tmp_path, rec).build(_spec())
    assert len(rec.readiness_unit_texts) == 1
    after_lines = [
        line for line in rec.readiness_unit_texts[0].splitlines() if line.startswith("After=")
    ]
    assert any("kdump-tools.service" in line for line in after_lines), (
        "the rendered kdive-ready unit must be ordered After= the family's kdump unit, not a "
        "hardcoded one"
    )


def test_provenance_source_digest_for_virt_builder_entry(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec(name="fedora-kdive-ready-43", releasever="42"))
    entry = resolve_rootfs_entry("fedora-kdive-ready-43")
    assert isinstance(entry.source, VirtBuilderSource)
    assert out.provenance == {
        "plane": "local-libvirt",
        "distro": "fedora",
        "releasever": "42",
        "packages": ["openssh-server", "drgn"],
        "source_image_digest": f"virt-builder:{entry.source.template}",
        "capabilities": ["agent", "kdump", "drgn"],
        "arch": "x86_64",
        "image_size": "6G",
        "readiness_marker": "kdive-ready",
        "layout": "whole-disk-ext4-qcow2",
        "guest_mac": "selinux-permissive",
    }
    assert rec.acquired_sources == [entry.source], "the catalog source is acquired"


def test_provenance_source_digest_for_cloud_image_entry(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec(name="fedora-kdive-ready-44", releasever="44"))
    entry = resolve_rootfs_entry("fedora-kdive-ready-44")
    assert isinstance(entry.source, CloudImageSource)
    expected = f"cloud-image:{entry.source.url}@sha256:{entry.source.sha256}"
    assert out.provenance["source_image_digest"] == expected
    assert rec.acquired_sources == [entry.source]


def test_provenance_records_package_versions(tmp_path: Path) -> None:
    # The inspector reports a superset; provenance keeps only the requested packages' versions.
    rec = _Recorder()
    versions = {"openssh-server": "9.6", "drgn": "0.0.28", "glibc": "2.39"}
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: versions).build(_spec())
    assert out.provenance["package_versions"] == {"openssh-server": "9.6", "drgn": "0.0.28"}
    assert out.provenance["packages"] == ["openssh-server", "drgn"], "the name list is unchanged"


def test_provenance_omits_versions_on_inspector_failure(tmp_path: Path) -> None:
    def _boom(_q: Path) -> dict[str, str]:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    out = _plane(tmp_path, rec, inspect_versions=_boom).build(_spec())
    assert "package_versions" not in out.provenance, "a failed capture degrades to an omitted field"


def test_provenance_versions_absent_for_unreported_request(tmp_path: Path) -> None:
    # A requested package the inspector does not report is absent from the map (not null/empty).
    rec = _Recorder()
    out = _plane(tmp_path, rec, inspect_versions=lambda _q: {"drgn": "0.0.28"}).build(_spec())
    assert out.provenance["package_versions"] == {"drgn": "0.0.28"}
    # openssh-server is still requested (in packages), just unversioned (not in the map).
    assert out.provenance["packages"] == ["openssh-server", "drgn"]


def test_provenance_records_makedumpfile_version_from_probe(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_makedumpfile=lambda _q: "makedumpfile: version 1.7.9 (released 2026-04-20)",
    ).build(_spec())
    assert out.provenance["makedumpfile_version"] == "1.7.9"


def test_provenance_makedumpfile_falls_back_to_package_versions(tmp_path: Path) -> None:
    # EL-style: the binary probe finds nothing; the standalone-package version is the fallback.
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        inspect_versions=lambda _q: {"makedumpfile": "1.7.2", "drgn": "0.0.28"},
        probe_makedumpfile=_no_makedumpfile,
    ).build(_spec())
    assert out.provenance["makedumpfile_version"] == "1.7.2"


def test_provenance_omits_makedumpfile_version_when_both_empty(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())
    assert "makedumpfile_version" not in out.provenance


def test_provenance_omits_makedumpfile_version_on_probe_error(tmp_path: Path) -> None:
    def _boom(_q: Path) -> str | None:
        raise CategorizedError("no tool", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_makedumpfile=_boom).build(_spec())
    assert "makedumpfile_version" not in out.provenance


def test_provenance_omits_makedumpfile_version_on_unparseable_probe(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_makedumpfile=lambda _q: "garbage output").build(_spec())
    assert "makedumpfile_version" not in out.provenance


def test_provenance_records_drgn_version_from_probe(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_drgn=lambda _q: "drgn 0.0.31").build(_spec())
    assert out.provenance["drgn_version"] == "0.0.31"


def test_provenance_drgn_falls_back_to_package_versions(tmp_path: Path) -> None:
    # The marker is empty; the installed drgn package version is the fallback (Fedora/EL: drgn).
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        inspect_versions=lambda _q: {"drgn": "0.0.28"},
        probe_drgn=_no_drgn,
    ).build(_spec())
    assert out.provenance["drgn_version"] == "0.0.28"


def test_provenance_drgn_falls_back_to_python3_drgn_package(tmp_path: Path) -> None:
    # Debian ships drgn as python3-drgn; the fallback consults that name too.
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        inspect_versions=lambda _q: {"python3-drgn": "0.0.22"},
        probe_drgn=_no_drgn,
    ).build(_spec())
    assert out.provenance["drgn_version"] == "0.0.22"


def test_provenance_omits_drgn_version_when_both_empty(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())
    assert "drgn_version" not in out.provenance


def test_provenance_omits_drgn_version_on_probe_error(tmp_path: Path) -> None:
    def _boom(_q: Path) -> str | None:
        raise CategorizedError("no tool", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_drgn=_boom).build(_spec())
    assert "drgn_version" not in out.provenance


def test_provenance_omits_drgn_version_on_unparseable_probe(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_drgn=lambda _q: "no version here").build(_spec())
    assert "drgn_version" not in out.provenance


_ONE_KERNEL = ["vmlinuz-6.19.10-300.fc44.x86_64", "initramfs-6.19.10-300.fc44.x86_64.img"]
_TWO_KERNELS = ["vmlinuz-6.19.10-300.fc44.x86_64", "vmlinuz-6.18.0-100.fc44.x86_64"]
_RESCUE_AND_ONE = ["vmlinuz-6.19.10-300.fc44.x86_64", "vmlinuz-0-rescue-abc"]


def test_provenance_records_boot_kernel_count_multiple(tmp_path: Path) -> None:
    # A multi-kernel /boot records the true count so images.describe can report not_provisionable.
    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_boot_entries=lambda _q: list(_TWO_KERNELS)).build(_spec())
    assert out.provenance["boot_kernel_count"] == 2


def test_provenance_records_boot_kernel_count_one_excluding_rescue(tmp_path: Path) -> None:
    rec = _Recorder()
    single = _plane(tmp_path, rec, probe_boot_entries=lambda _q: list(_ONE_KERNEL)).build(_spec())
    assert single.provenance["boot_kernel_count"] == 1

    rec2 = _Recorder()
    with_rescue = _plane(tmp_path, rec2, probe_boot_entries=lambda _q: list(_RESCUE_AND_ONE)).build(
        _spec()
    )
    assert with_rescue.provenance["boot_kernel_count"] == 1, "the rescue kernel is not counted"


def test_provenance_records_boot_kernel_count_zero_and_keeps_the_key(tmp_path: Path) -> None:
    # A kernel-less /boot is a meaningful "not provisionable" operand: 0 is recorded, not dropped.
    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_boot_entries=lambda _q: ["config-x", "grub2"]).build(_spec())
    assert out.provenance["boot_kernel_count"] == 0


def test_provenance_omits_boot_kernel_count_when_probe_returns_none(tmp_path: Path) -> None:
    # The hermetic default (_no_boot_entries -> None) is the omitted-operand path.
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())
    assert "boot_kernel_count" not in out.provenance


def test_provenance_omits_boot_kernel_count_on_probe_error(tmp_path: Path) -> None:
    def _boom(_q: Path) -> list[str] | None:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_boot_entries=_boom).build(_spec())
    assert "boot_kernel_count" not in out.provenance


def test_single_kernel_captures_version_and_config(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_boot_entries=lambda _q: list(_ONE_KERNEL),
        probe_kernel_config=lambda _q, ver: f"# config for {ver}\nCONFIG_X=y\n".encode(),
    ).build(_spec())
    assert out.provenance["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
    assert out.kernel_config == b"# config for 6.19.10-300.fc44.x86_64\nCONFIG_X=y\n"


def test_multi_kernel_omits_version_and_config(tmp_path: Path) -> None:
    def _must_not_probe(_q: Path, _ver: str) -> bytes | None:
        raise AssertionError("kernel-config probe must not run for an ambiguous multi-kernel /boot")

    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_boot_entries=lambda _q: list(_TWO_KERNELS),
        probe_kernel_config=_must_not_probe,
    ).build(_spec())
    assert "default_kernel_version" not in out.provenance
    assert out.kernel_config is None


def test_config_absent_keeps_version_drops_config(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_boot_entries=lambda _q: list(_ONE_KERNEL),
        probe_kernel_config=lambda _q, _ver: None,
    ).build(_spec())
    assert out.provenance["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
    assert out.kernel_config is None


def test_config_probe_error_degrades_but_keeps_version(tmp_path: Path) -> None:
    def _boom(_q: Path, _ver: str) -> bytes | None:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_boot_entries=lambda _q: list(_ONE_KERNEL),
        probe_kernel_config=_boom,
    ).build(_spec())
    assert out.provenance["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
    assert out.kernel_config is None


def test_rescue_plus_one_kernel_captures_the_non_rescue_version(tmp_path: Path) -> None:
    rec = _Recorder()
    out = _plane(
        tmp_path,
        rec,
        probe_boot_entries=lambda _q: list(_RESCUE_AND_ONE),
        probe_kernel_config=lambda _q, ver: f"CONFIG_FOR={ver}\n".encode(),
    ).build(_spec())
    assert out.provenance["default_kernel_version"] == "6.19.10-300.fc44.x86_64"
    assert out.kernel_config == b"CONFIG_FOR=6.19.10-300.fc44.x86_64\n"


def test_parse_os_release_quoted_and_unquoted() -> None:
    text = 'ID=fedora\nVERSION_ID=43\nPRETTY_NAME="Fedora Linux 43"\n'
    assert _parse_os_release(text) == {
        "id": "fedora",
        "version_id": "43",
        "pretty_name": "Fedora Linux 43",
    }


def test_parse_os_release_single_quotes() -> None:
    assert _parse_os_release("ID='sles'\nVERSION_ID='15'\n") == {"id": "sles", "version_id": "15"}


def test_parse_os_release_id_only_partial() -> None:
    # A rolling distro (e.g. Debian testing) may ship ID with no VERSION_ID — a valid record.
    assert _parse_os_release("ID=debian\n") == {"id": "debian"}


def test_parse_os_release_missing_id_returns_none() -> None:
    assert _parse_os_release('PRETTY_NAME="X"\nVERSION_ID=1\n') is None


def test_parse_os_release_blank_id_returns_none() -> None:
    # A present-but-empty ID is not a usable identity; do not record os_release={"id": ""}.
    assert _parse_os_release("ID=\n") is None
    assert _parse_os_release('ID=""\nVERSION_ID=43\n') is None


def test_parse_os_release_skips_comments_and_blanks() -> None:
    assert _parse_os_release("# a comment\n\nID=rocky\n") == {"id": "rocky"}


def test_parse_os_release_ignores_malformed_lines() -> None:
    assert _parse_os_release("garbage-no-equals\nID=x\n") == {"id": "x"}


def test_parse_os_release_all_malformed_returns_none() -> None:
    assert _parse_os_release("nonsense-line\n") is None


def test_provenance_records_os_release(tmp_path: Path) -> None:
    rec = _Recorder()
    text = 'ID=fedora\nVERSION_ID=43\nPRETTY_NAME="Fedora Linux 43"\n'
    out = _plane(tmp_path, rec, probe_os_release=lambda _q: text).build(_spec())
    assert out.provenance["os_release"] == {
        "id": "fedora",
        "version_id": "43",
        "pretty_name": "Fedora Linux 43",
    }


def test_provenance_omits_os_release_when_probe_returns_none(tmp_path: Path) -> None:
    # The hermetic default (_no_os_release -> None) is the omitted-operand path.
    rec = _Recorder()
    out = _plane(tmp_path, rec).build(_spec())
    assert "os_release" not in out.provenance


def test_provenance_omits_os_release_on_probe_error(tmp_path: Path) -> None:
    def _boom(_q: Path) -> str | None:
        raise CategorizedError("no tool", category=ErrorCategory.MISSING_DEPENDENCY)

    rec = _Recorder()
    out = _plane(tmp_path, rec, probe_os_release=_boom).build(_spec())
    assert "os_release" not in out.provenance


def test_build_rejects_uncataloged_name(tmp_path: Path) -> None:
    # The provider plane no longer synthesizes old-style virt-builder specs. New images must be
    # real catalog entries so build-fs has one contract and catalog provenance stays falsifiable.
    rec = _Recorder()
    spec = _spec(name="legacy-image-99", distro="fedora", releasever="41")
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, rec).build(spec)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert rec.acquired_sources == []


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", ".hidden", "-leading", "with space"])
def test_build_rejects_a_name_that_would_escape_the_workspace(
    tmp_path: Path, bad_name: str
) -> None:
    rec = _Recorder()
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, rec).build(_spec(name=bad_name))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert rec.order == [], "an unsafe name is rejected before any build stage runs"


def test_family_for_resolves_rhel_and_rejects_unknown() -> None:
    assert isinstance(family_for("rhel"), RhelFamily)
    with pytest.raises(CategorizedError) as exc:
        family_for("plan9")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["family"] == "plan9"


def _rhel_argv(
    tmp_path: Path,
    *,
    packages: tuple[str, ...],
    is_cloud_image: bool = False,
    distro: str = "fedora",
    version: str = "44",
) -> list[str]:
    """Build the rhel customizer argv the plane feeds virt-customize, without running libguestfs."""
    ctx = CustomizeContext(
        kind="debug",
        packages=packages,
        readiness_unit_path=tmp_path / "kdive-ready.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro=distro,
        version=version,
    )
    return render_argv(RhelFamily().customize_steps(ctx), cleanup=[])


def _upload_target(argv: list[str], guest_path: str) -> str:
    """Return the host source of the `--upload <src>:<guest_path>` arg, or fail if absent."""
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag == "--upload" and value.endswith(f":{guest_path}"):
            return value.rsplit(":", 1)[0]
    raise AssertionError(f"no --upload arg targets {guest_path}: {argv}")


def test_family_argv_omits_nmi_panic_sysctl_for_a_non_kdump_image(tmp_path: Path) -> None:
    # A non-kdump (e.g. build-host) image never runs force_crash; a stray NMI must not panic it,
    # so the sysctl is gated on the same kexec-tools condition that enables kdump.service (#823).
    joined = " ".join(_rhel_argv(tmp_path, packages=("gcc", "make")))
    assert "unknown_nmi_panic" not in joined
    assert "99-kdive-kdump.conf" not in joined
    assert "final_action" not in joined


def test_family_argv_stages_kdive_drgn_helper_for_a_debug_image(tmp_path: Path) -> None:
    # The live `introspect.run` path SSH-execs `/usr/local/sbin/kdive-drgn <helper>` in the guest
    # (ADR-0219/0220, #724). The debug image (drgn in packages) stages the repo's reviewed reference
    # helper read-executable so a live attach can run it; absent → DEBUG_ATTACH_FAILURE.
    argv = _rhel_argv(tmp_path, packages=("drgn",))
    helper_src = _upload_target(argv, "/usr/local/sbin/kdive-drgn")
    assert helper_src.endswith("deploy/remote-libvirt-guest-helpers/kdive-drgn")
    assert "chmod 0755 /usr/local/sbin/kdive-drgn" in argv, "helper is made read-executable"


def test_family_argv_omits_drgn_helper_for_a_non_debug_image(tmp_path: Path) -> None:
    # A non-debug (e.g. build-host) image carries no drgn and no introspection contract, so it gets
    # no kdive-drgn helper — gated on `drgn in packages`.
    joined = " ".join(_rhel_argv(tmp_path, packages=("gcc", "make")))
    assert "kdive-drgn" not in joined


def test_family_argv_fails_loud_when_drgn_helper_source_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The helper is resolved from the source tree; an absent helper file must fail loud with a
    # CONFIGURATION_ERROR rather than ship a guest that cannot introspect (ADR-0220 D2, #724).
    import kdive.images.families._fedora_customize as fedora_customize

    monkeypatch.setattr(fedora_customize, "drgn_helper_source", lambda: tmp_path / "missing")
    with pytest.raises(CategorizedError) as exc:
        _rhel_argv(tmp_path, packages=("drgn",))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- Task 9: the customization-boot build path (ADR-0345) -----------------------------------------


@dataclass
class _RecordingBootFamily:
    """A FamilyCustomizer stub whose ``customize_via`` drives the build plane's dispatch."""

    rec: _RecordingBootTools
    family: str = "rhel"
    kdump_unit: str = "kdump.service"
    guest_mac: str = "selinux-permissive"
    customize_via: str = "boot"

    def packages(self, kind: str, distro: str, version: str) -> tuple[str, ...]:
        return ("marker-pkg",)

    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        return (Capability.SSH,)

    def customize_steps(self, ctx: CustomizeContext) -> list[Step]:
        # A mix of file-ops (partitioned to inject_offline) and exec-ops (the firstboot script),
        # plus the plane-rendered kdive-ready unit upload — read it to guard the point-6 wiring.
        self.rec.readiness_unit_texts.append(ctx.readiness_unit_path.read_text())
        return [
            InstallPackages(("marker-pkg",)),
            RunCommand("marker-customize"),
            WriteFile("/etc/kdive-marker", "x"),
            UploadFile(ctx.readiness_unit_path, "/etc/systemd/system/kdive-ready.service"),
        ]

    def normalize(self, qcow2: Path, *, relabel: bool = True) -> None:
        self.rec.order.append("normalize")
        self.rec.normalize_relabel = relabel


@dataclass
class _RecordingBootTools:
    """Injected toolset recording the boot-path build; no libvirt/guestfs is touched."""

    accel: tuple[str, str | None] = ("kvm", None)
    boot_raises: CategorizedError | None = None
    order: list[str] = field(default_factory=list)
    readiness_unit_texts: list[str] = field(default_factory=list)
    staged_path: Path | None = None
    probed_path: Path | None = None
    customization_boot_ran: bool = False
    virt_customize_ran: bool = False
    boot_accel: str | None = None
    boot_domain_name: str | None = None
    inject_file_ops: list[Step] = field(default_factory=list)
    inject_script: str | None = None
    normalize_relabel: bool | None = None
    sealed: bool = False
    seal_selinux: bool | None = None
    payload: bytes = b"qcow2-bytes"

    def family_for(self, name: str) -> _RecordingBootFamily:
        if name == "debian":
            return _RecordingBootFamily(
                self,
                family="debian",
                kdump_unit="kdump-tools.service",
                guest_mac="apparmor",
                customize_via="virt_customize",
            )
        return _RecordingBootFamily(self)

    def acquire_base(
        self,
        source: RootfsSource,
        scratch: Path,
        *,
        releasever: str,
        arch: str,
        virt_builder: object,
        downloader: object,
    ) -> None:
        scratch.write_bytes(b"scratch")
        self.order.append("acquire")

    def customize(self, qcow2: Path, argv: list[str]) -> None:
        self.virt_customize_ran = True
        self.order.append("customize")

    def repack_whole_disk_ext4(self, *, scratch: Path, qcow2: Path, size: str) -> None:
        qcow2.write_bytes(self.payload)
        self.staged_path = qcow2
        self.order.append("repack")

    def inject_offline(
        self, qcow2: Path, file_ops: list[Step], firstboot_script: str, firstboot_unit: str
    ) -> None:
        self.inject_file_ops = list(file_ops)
        self.inject_script = firstboot_script
        self.order.append("inject")

    def extract_baseline_kernel(
        self, base: Path, dest_dir: Path, hint: str | None
    ) -> BaselineKernel:
        return BaselineKernel(kernel=dest_dir / "kernel", initrd=None)

    def resolve_accel(self, arch: str) -> tuple[str, str | None]:
        return self.accel

    def run_customization_boot(self, build_id: object, domain_xml: str, *, accel: str) -> None:
        self.customization_boot_ran = True
        self.boot_accel = accel
        self.boot_domain_name = ET.fromstring(domain_xml).findtext("name")
        self.order.append("boot")
        if self.boot_raises is not None:
            raise self.boot_raises

    def seal_customized_image(self, qcow2: Path, *, unit_name: str, selinux: bool) -> None:
        self.sealed = True
        self.seal_selinux = selinux
        self.order.append("seal")

    def verify_cloud_init(self, qcow2: Path) -> None:
        self.order.append("verify")

    def inspect_versions(self, qcow2: Path) -> dict[str, str]:
        self.probed_path = qcow2
        return {}

    def as_tools(self) -> RootfsBuildTools:
        return RootfsBuildTools(
            acquire_base=self.acquire_base,
            customize=self.customize,
            repack_whole_disk_ext4=self.repack_whole_disk_ext4,
            family_for=self.family_for,
            inject_offline=self.inject_offline,
            extract_baseline_kernel=self.extract_baseline_kernel,
            resolve_accel=self.resolve_accel,
            run_customization_boot=self.run_customization_boot,
            seal_customized_image=self.seal_customized_image,
            verify_cloud_init=self.verify_cloud_init,
            inspect_versions=self.inspect_versions,
            probe_makedumpfile=_no_makedumpfile,
            probe_drgn=_no_drgn,
            probe_boot_entries=_no_boot_entries,
            probe_os_release=_no_os_release,
            probe_kernel_config=_no_kernel_config,
        )


def test_rhel_build_uses_customization_boot(tmp_path: Path) -> None:
    calls = _RecordingBootTools(accel=("kvm", None))
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=calls.as_tools())
    plane.build(_spec(name="fedora-kdive-ready-44", arch="x86_64"))
    assert calls.customization_boot_ran
    assert not calls.virt_customize_ran, "the boot path never runs virt-customize"
    assert calls.boot_accel == "kvm", "the resolve_accel seam drove the accelerator branch"
    assert calls.boot_domain_name is not None
    assert calls.boot_domain_name.startswith("kdive-build-")
    assert calls.normalize_relabel is False, "the boot path normalizes without /.autorelabel"
    assert calls.sealed and calls.seal_selinux is True, "selinux family seals with a relabel"
    assert calls.probed_path == calls.staged_path, "provenance is probed from staged, not scratch"
    # The firstboot script carries the exec-ops; the file-ops go to inject_offline.
    assert calls.inject_script is not None
    assert "dnf -y install marker-pkg" in calls.inject_script
    assert any(isinstance(op, WriteFile) for op in calls.inject_file_ops)
    assert not any(isinstance(op, InstallPackages) for op in calls.inject_file_ops)


def test_ppc64le_build_boots_under_tcg(tmp_path: Path) -> None:
    calls = _RecordingBootTools(accel=("tcg", "/usr/bin/qemu-system-ppc64"))
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=calls.as_tools())
    plane.build(_spec(name="fedora-kdive-ready-44-ppc64le", arch="ppc64le"))
    assert calls.boot_accel == "tcg", "the TCG branch is unit-covered"
    assert calls.customization_boot_ran


def test_debian_build_stays_on_virt_customize(tmp_path: Path) -> None:
    calls = _RecordingBootTools()
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=calls.as_tools())
    plane.build(_spec(name="debian-kdive-ready-13", arch="x86_64", distro="debian"))
    assert calls.virt_customize_ran
    assert not calls.customization_boot_ran, "debian is unchanged (virt-customize path)"
    assert calls.normalize_relabel is True, "the virt-customize path normalizes with relabel"


def test_boot_failure_aborts_publish(tmp_path: Path) -> None:
    calls = _RecordingBootTools(
        boot_raises=CategorizedError("dnf failed", category=ErrorCategory.PROVISIONING_FAILURE)
    )
    plane = LocalLibvirtRootfsBuildPlane(workspace=tmp_path / "work", tools=calls.as_tools())
    with pytest.raises(CategorizedError) as err:
        plane.build(_spec(name="fedora-kdive-ready-44"))
    assert err.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert not calls.sealed, "a failed boot never reaches seal"
    assert not (tmp_path / "work" / "fedora-kdive-ready-44.qcow2").exists(), "publish did not run"
