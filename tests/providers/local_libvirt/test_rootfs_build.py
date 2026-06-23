"""Unit tests for the in-process local-libvirt rootfs build plane (M2.4/2, ADR-0092).

These cover the plane's orchestration and provenance contract without libguestfs or qemu: the
slow tools (`virt-builder`, `virt-tar-out`, `virt-make-fs`, `guestfish`) are injected seams the
tests stub. The real libguestfs path is exercised on the operator-run live-stack path. The
acceptance that the produced qcow2 passes `virt-inspector` for the expected layout (whole-disk
ext4, normalized fstab, no crypttab, guest SELinux off) is asserted by recording the guest-side
operations the plane drives — the live path proves the layout, the unit path proves the wiring.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.providers.local_libvirt import rootfs_build
from kdive.providers.local_libvirt.rootfs_build import (
    LocalLibvirtRootfsBuildPlane,
    RootfsBuildTools,
    _real_virt_builder,
)


def _spec(**overrides: object) -> RootfsBuildSpec:
    base: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "fedora-kdive-ready-43",
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("openssh-server", "drgn"),
        "source_image_digest": "sha256:fedora-43-template",
        "capabilities": ("agent", "kdump", "drgn"),
    }
    base.update(overrides)
    return RootfsBuildSpec(**base)  # ty: ignore[invalid-argument-type]


@dataclass
class _RecordingTools:
    """Stub seams that record the guest-side operations the plane drives."""

    authorized_key: Path
    builder_calls: list[dict[str, object]] = field(default_factory=list)
    repack_calls: list[tuple[Path, Path]] = field(default_factory=list)
    normalize_calls: list[Path] = field(default_factory=list)
    payload: bytes = b"qcow2-bytes"

    def resolve_authorized_key(self) -> Path:
        return self.authorized_key

    def virt_builder(
        self,
        *,
        distro: str,
        releasever: str,
        packages: tuple[str, ...],
        authorized_key: Path,
        scratch: Path,
        size: str,
    ) -> None:
        scratch.write_bytes(b"scratch")
        self.builder_calls.append(
            {
                "distro": distro,
                "releasever": releasever,
                "packages": packages,
                "authorized_key": authorized_key,
                "size": size,
            }
        )

    repack_sizes: list[str] = field(default_factory=list)

    def repack_whole_disk_ext4(self, *, scratch: Path, qcow2: Path, size: str) -> None:
        qcow2.write_bytes(self.payload)
        self.repack_calls.append((scratch, qcow2))
        self.repack_sizes.append(size)

    def normalize_guest(self, qcow2: Path) -> None:
        self.normalize_calls.append(qcow2)


def _plane(tmp_path: Path, tools: _RecordingTools) -> LocalLibvirtRootfsBuildPlane:
    return LocalLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=RootfsBuildTools(
            resolve_authorized_key=tools.resolve_authorized_key,
            virt_builder=tools.virt_builder,
            repack_whole_disk_ext4=tools.repack_whole_disk_ext4,
            normalize_guest=tools.normalize_guest,
        ),
    )


def test_default_workspace_is_the_managed_build_path() -> None:
    # With no workspace override the plane defaults to the managed images path, not a
    # null/derived location; from_env carries that same default through.
    assert LocalLibvirtRootfsBuildPlane()._workspace == Path("/var/lib/kdive/build/images")
    assert LocalLibvirtRootfsBuildPlane.from_env()._workspace == Path("/var/lib/kdive/build/images")


def test_build_produces_qcow2_with_content_digest(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key, payload=b"the-image-bytes")
    out = _plane(tmp_path, tools).build(_spec())

    assert isinstance(out, RootfsBuildOutput)
    assert out.qcow2_path.exists()
    assert out.qcow2_path.read_bytes() == b"the-image-bytes"
    expected = "sha256:" + hashlib.sha256(b"the-image-bytes").hexdigest()
    assert out.digest == expected, "image identity is the qcow2 content digest"


def test_build_records_pinned_provenance(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    out = _plane(tmp_path, tools).build(_spec(releasever="42", packages=("openssh-server",)))

    # The provenance record is the plane's falsifiable contract (ADR-0092): pin every
    # key and value so a dropped/renamed field or a swapped-in default is caught.
    assert out.provenance == {
        "plane": "local-libvirt",
        "distro": "fedora",
        "releasever": "42",
        "packages": ["openssh-server"],
        "source_image_digest": "sha256:fedora-43-template",
        "capabilities": ["agent", "kdump", "drgn"],
        "arch": "x86_64",
        "image_size": "6G",
        "authorized_key_name": "id.pub",
        "readiness_marker": "kdive-ready",
        "layout": "whole-disk-ext4-qcow2",
        "guest_selinux": "disabled",
    }


def test_build_drives_the_layout_stages_in_order(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    out = _plane(tmp_path, tools).build(_spec())

    assert len(tools.builder_calls) == 1, "virt-builder customizes the scratch image once"
    assert tools.builder_calls[0]["distro"] == "fedora"
    assert tools.builder_calls[0]["releasever"] == "43"
    assert tools.builder_calls[0]["packages"] == ("openssh-server", "drgn")
    assert tools.builder_calls[0]["authorized_key"] == key
    assert tools.builder_calls[0]["size"] == "6G", "configured size flows to virt-builder"
    assert len(tools.repack_calls) == 1, "repacked to a whole-disk ext4 qcow2 once"
    scratch_path, staged_qcow2 = tools.repack_calls[0]
    assert scratch_path.name == "scratch.qcow2", "the customized scratch image is repacked"
    assert tools.repack_sizes == ["6G"], "the configured size flows to the repack stage"
    assert tools.normalize_calls == [staged_qcow2], (
        "fstab/crypttab/SELinux normalized before publish"
    )
    assert out.qcow2_path.name == staged_qcow2.name


def test_build_fails_fast_when_authorized_key_unresolved(tmp_path: Path) -> None:
    key = tmp_path / "missing.pub"  # never created
    tools = _RecordingTools(authorized_key=key)
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, tools).build(_spec())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == (
        "resolved SSH public key is not a readable file; cannot build the rootfs image"
    )
    assert exc.value.details == {"authorized_key": str(key)}
    assert not tools.builder_calls, "no libguestfs stage runs without a resolvable key"


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", ".hidden", "-leading", "with space"])
def test_build_rejects_a_name_that_would_escape_the_workspace(
    tmp_path: Path, bad_name: str
) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, tools).build(_spec(name=bad_name))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not tools.builder_calls, "an unsafe name is rejected before any libguestfs stage runs"


def _capture_virt_builder_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, packages: tuple[str, ...]
) -> list[str]:
    """Drive the real virt-builder seam with a stubbed runner and return the argv it built."""
    captured: list[list[str]] = []

    def _fake_run_guestfs_tool(argv: list[str], **_: object) -> None:
        captured.append(argv)

    monkeypatch.setattr(rootfs_build, "run_guestfs_tool", _fake_run_guestfs_tool)
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    _real_virt_builder(
        distro="fedora",
        releasever="43",
        packages=packages,
        authorized_key=key,
        scratch=tmp_path / "scratch.qcow2",
        size="6G",
    )
    assert len(captured) == 1, "virt-builder runs once"
    return captured[0]


def test_virt_builder_stages_nmi_panic_sysctl_for_a_kdump_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A kdump image must panic on the NMI `control.force_crash` injects; without this sysctl the
    # guest ignores it and kdump never triggers (ADR-0213, #688, mirrors remote ADR-0084).
    argv = _capture_virt_builder_argv(monkeypatch, tmp_path, packages=("kdump-utils",))
    assert "--write" in argv
    write_value = argv[argv.index("--write") + 1]
    assert write_value == "/etc/sysctl.d/99-kdive-kdump.conf:kernel.unknown_nmi_panic=1\n"


def test_virt_builder_pins_kdump_final_action_to_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host-side harvest waits for the guest to self-shut-off after dumping (ADR-0217); pinning
    # kdump `final_action shutdown` makes VIR_DOMAIN_SHUTOFF the reliable completion signal instead
    # of Fedora's default `reboot`, which never self-shuts-off.
    argv = _capture_virt_builder_argv(monkeypatch, tmp_path, packages=("kdump-utils",))
    joined = " ".join(argv)
    assert "final_action shutdown" in joined
    assert "/etc/kdump.conf" in joined


def test_virt_builder_omits_nmi_panic_sysctl_for_a_non_kdump_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-kdump (e.g. build-host) image never runs force_crash; a stray NMI must not panic it,
    # so the sysctl is gated on the same kdump-utils condition that enables kdump.service.
    argv = _capture_virt_builder_argv(monkeypatch, tmp_path, packages=("gcc", "make"))
    joined = " ".join(argv)
    assert "unknown_nmi_panic" not in joined
    assert "99-kdive-kdump.conf" not in joined
    assert "final_action" not in joined
