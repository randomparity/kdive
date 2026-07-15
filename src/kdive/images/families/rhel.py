"""The rhel-family (Fedora/RHEL) rootfs FamilyCustomizer (ADR-0251, ADR-0345).

Emits the ordered customization ``Step``s PROVEN live on Fedora 44 in the #817 de-risk spike:
install the dnf package set, enable ``sshd``/``kdump``, write the NMI-panic sysctl, pin kdump
``final_action poweroff``, stage the debug-image drgn helper, upload+enable the kdive-ready
serial-readiness unit, and set SELinux permissive. It also enables cloud-init via a baked NoCloud
seed (ADR-0288), the uniform rootfs first-boot mechanism. ``customize_via = "boot"``: the build
plane boots the image and lets it self-customize (ADR-0345). The image bakes no authorized key
(ADR-0289, #963); the per-System bootstrap key is injected at provision time.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from kdive.domain.catalog.images import Capability
from kdive.images.families._fedora_customize import (
    DEFAULT_BUILD_FS_PACKAGES,
    DEFAULT_DEBUG_FS_PACKAGES,
    FSTAB,
    KDUMP_FINAL_ACTION_CMD,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    cloud_init_first_boot_steps,
    debug_image_steps,
    drgn_version_marker_steps,
    makedumpfile_version_marker_steps,
)
from kdive.images.families.base import CustomizeContext, _mac_tag
from kdive.images.families.steps import (
    InstallPackages,
    RunCommand,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.images.planes._build_common import run_guestfs_tool
from kdive.images.rootfs.kinds import RootfsImageKind

# Cloud-image SELinux ships enforcing; the bare-ext4 repack drops xattrs, so a relabel-on-boot is
# required. Set permissive so the first boot relabels (``/.autorelabel``) without denying the
# host-written authorized_keys, matching the spike-proven F44 image.
_SELINUX_PERMISSIVE_SED = "sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config"
_SELINUX_PERMISSIVE_CONFIG = "SELINUX=permissive\nSELINUXTYPE=targeted\n"
_GUESTFISH_TIMEOUT_S = 5 * 60

# EL 8/9 bundle makedumpfile + ``kdumpctl`` inside ``kexec-tools`` (no standalone ``makedumpfile``
# / ``kdump-utils`` packages); installing those names fails the build. The debug set there is just
# the crash/introspection tools plus the live-attach ``openssh-server``.
_EL8_EL9_DEBUG_PACKAGES = ("drgn", "kexec-tools", "keyutils", "openssh-server")
# ``drgn`` is not in EL BaseOS/AppStream on any EL major (8/9/10); it ships in EPEL, so every EL
# clone needs EPEL enabled before the ``drgn`` install (#1152 corrects this — it was EL8-only, which
# left EL9/EL10 unable to install drgn; latent until the first EL9 customize boot). ``epel-release``
# is in the default-enabled extras repo on Rocky (``extras``) and CentOS Stream (``extras-common``),
# so installing it needs no prior repo enable. Run as a separate transaction *before* the ``drgn``
# install so the EPEL repo metadata is present (ADR-0251, #823, ADR-0350).
_ENABLE_EPEL_CMD = "dnf -y install epel-release"


def _el_major(distro: str, version: str) -> int | None:
    """Return the EL major for an EL-clone distro, or ``None`` for Fedora / an unparsable version.

    ``None`` means "treat like Fedora": the modern layout where ``makedumpfile``/``kdump-utils``
    are standalone packages (EL >= 10 and Fedora).
    """
    if distro == "fedora":
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


class RhelFamily:
    """The rhel-family (dnf + kdump) :class:`FamilyCustomizer`, EL-major-aware (#823)."""

    family = "rhel"
    kdump_unit = "kdump.service"
    guest_mac = "selinux-permissive"
    customize_via: Literal["boot", "virt_customize"] = "boot"

    def packages(self, kind: RootfsImageKind, distro: str, version: str) -> tuple[str, ...]:
        """Return the dnf package set for ``kind`` on ``distro``/``version``.

        ``build`` returns the kernel-build toolchain (release-independent). A debug image returns
        the crash/introspection set plus ``openssh-server`` (the live-attach transport); on EL 8/9
        the standalone ``makedumpfile``/``kdump-utils`` are dropped (bundled in ``kexec-tools``),
        on Fedora and EL >= 10 they are kept.
        """
        if kind == "build":
            return DEFAULT_BUILD_FS_PACKAGES
        major = _el_major(distro, version)
        if major is not None and major <= 9:
            return _EL8_EL9_DEBUG_PACKAGES
        return (*DEFAULT_DEBUG_FS_PACKAGES, "openssh-server")

    def capabilities(
        self, kind: RootfsImageKind, distro: str, version: str
    ) -> tuple[Capability, ...]:
        """Return the tags this family bakes. EL-major-invariant, so distro/version unused."""
        del distro, version
        mac = _mac_tag(self.guest_mac)
        if kind == "build":
            return (mac, Capability.BUILD)
        return (Capability.SSH, mac, Capability.KDUMP, Capability.DRGN)

    def customize_steps(self, ctx: CustomizeContext) -> list[Step]:
        """Build the ordered steps that turn the base image into a kdive-ready rootfs."""
        steps: list[Step] = []
        # Every EL clone (major is not None) takes drgn from EPEL; Fedora (None) ships it in base.
        if _el_major(ctx.distro, ctx.version) is not None and "drgn" in ctx.packages:
            steps.append(RunCommand(_ENABLE_EPEL_CMD))
        steps.append(InstallPackages(ctx.packages))
        # Enable sshd exactly when this image declares the SSH capability, which ``capabilities()``
        # ties to ``kind`` (every debug image, never a build-host image). Gating on ``kind`` (not
        # package membership) keeps the declaration and the enable from diverging: a debug image
        # that somehow lacks openssh-server fails the build loudly here rather than shipping an
        # ``ssh``-tagged image with no sshd.
        if ctx.kind == "debug":
            steps.append(RunCommand("systemctl enable sshd.service"))
        # Gate on ``kexec-tools`` (in every debug set, absent from the build set), not the
        # Fedora-only ``kdump-utils`` — EL 8/9 get kdump from ``kexec-tools`` (#823).
        if "kexec-tools" in ctx.packages:
            steps.append(RunCommand("systemctl enable kdump.service"))
            steps.append(WriteFile(KDUMP_SYSCTL_PATH, KDUMP_SYSCTL_CONTENT))
            steps.append(RunCommand(KDUMP_FINAL_ACTION_CMD))
        steps += cloud_init_first_boot_steps(ctx)
        steps += debug_image_steps(ctx.packages)
        if ctx.kind == "debug":
            steps += makedumpfile_version_marker_steps()
        # Record the shipped drgn version only when the introspection package is installed
        # (``drgn`` on rhel/fedora), so the ``live_drgn`` signal resolves for this built image
        # (ADR-0334); a build-host image with no drgn writes no marker.
        if "drgn" in ctx.packages:
            steps += drgn_version_marker_steps()
        steps.append(
            UploadFile(ctx.readiness_unit_path, f"/etc/systemd/system/{READINESS_MARKER}.service")
        )
        steps.append(RunCommand(f"systemctl enable {READINESS_MARKER}.service"))
        steps.append(RunCommand(_SELINUX_PERMISSIVE_SED))
        return steps

    def normalize(self, qcow2: Path, *, relabel: bool = True) -> None:
        """Normalize fstab/crypttab/SELinux, optionally forcing a first-boot relabel via guestfish.

        The tar->ext4 repack drops SELinux xattrs, so ``/.autorelabel`` forces a first-boot
        ``restorecon``; combined with SELINUX=permissive the guest boots and relabels rather than
        denying the host-written authorized_keys. The boot path passes ``relabel=False`` to skip
        the touch here — the customization boot runs before the relabel would, so the offline seal
        does the touch afterward instead (ADR-0345); the fstab/crypttab/SELINUX=permissive edits
        are unconditional.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fstab_handle:
            fstab_handle.write(FSTAB)
            fstab_path = Path(fstab_handle.name)
        with tempfile.NamedTemporaryFile("w", suffix=".selinux", delete=False) as selinux_handle:
            selinux_handle.write(_SELINUX_PERMISSIVE_CONFIG)
            selinux_path = Path(selinux_handle.name)
        script = (
            f"upload {fstab_path} /etc/fstab\n"
            f"upload {selinux_path} /etc/selinux/config\n"
            "rm-f /etc/crypttab\n"
        )
        if relabel:
            script += "touch /.autorelabel\n"
        try:
            run_guestfs_tool(
                ["guestfish", "--rw", "-a", str(qcow2), "-i"],
                stage="guestfish",
                timeout_s=_GUESTFISH_TIMEOUT_S,
                missing_message="guestfish is not installed; cannot normalize the rootfs image",
                failure_message="guestfish normalization failed",
                input_text=script,
            )
        finally:
            fstab_path.unlink(missing_ok=True)
            selinux_path.unlink(missing_ok=True)
