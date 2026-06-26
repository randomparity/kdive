"""The rhel-family (Fedora/RHEL) rootfs FamilyCustomizer (ADR-0250).

Encodes the virt-customize argv PROVEN live on Fedora 44 in the #817 de-risk spike: install the
dnf package set, enable ``sshd``/``kdump``, write the NMI-panic sysctl, pin kdump
``final_action poweroff``, stage the debug-image drgn/SSH-NIC helpers, inject the managed SSH key,
upload+enable the kdive-ready serial-readiness unit, and set SELinux permissive. For a cloud-image
base it additionally masks cloud-init and seeds ``/etc/machine-id`` (without the seed, Fedora
Cloud's ``machine-id=uninitialized`` triggers a first-boot ``preset-all`` that disables
kdump.service — proven failure ``kexec_crash_loaded=0``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.images.families._fedora_customize import (
    DEFAULT_BUILD_FS_PACKAGES,
    DEFAULT_DEBUG_FS_PACKAGES,
    FSTAB,
    KDUMP_FINAL_ACTION_CMD,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    debug_image_args,
)
from kdive.images.families.base import CustomizeContext
from kdive.images.planes._build_common import run_guestfs_tool

# A valid 32-hex machine-id seeded into cloud-image bases so the first boot does not run
# ``systemctl preset-all`` (which disables kdump.service on Fedora Cloud). Not a secret — a fixed
# build-time identity, intentionally constant.
_SEED_MACHINE_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"  # pragma: allowlist secret
_CLOUD_INIT_MASK = (
    "systemctl mask cloud-init.service cloud-init-local.service "
    "cloud-config.service cloud-final.service"
)
# Cloud-image SELinux ships enforcing; the bare-ext4 repack drops xattrs, so a relabel-on-boot is
# required. Set permissive so the first boot relabels (``/.autorelabel``) without denying the
# host-written authorized_keys, matching the spike-proven F44 image.
_SELINUX_PERMISSIVE_SED = "sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config"
_SELINUX_PERMISSIVE_CONFIG = "SELINUX=permissive\nSELINUXTYPE=targeted\n"
_GUESTFISH_TIMEOUT_S = 5 * 60


class RhelFamily:
    """The rhel-family (dnf + kdump-utils) :class:`FamilyCustomizer`."""

    family = "rhel"

    def packages(self, kind: str) -> tuple[str, ...]:
        """Return the dnf package set for ``kind``.

        ``build`` returns the kernel-build toolchain; any other kind returns the debug
        crash/introspection set plus ``openssh-server`` (the live-attach transport).
        """
        if kind == "build":
            return DEFAULT_BUILD_FS_PACKAGES
        return (*DEFAULT_DEBUG_FS_PACKAGES, "openssh-server")

    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Build the virt-customize argv that turns the base image into a kdive-ready rootfs."""
        argv: list[str] = [
            "--install",
            ",".join(ctx.packages),
            "--run-command",
            "systemctl enable sshd.service",
        ]
        if "kdump-utils" in ctx.packages:
            argv += [
                "--run-command",
                "systemctl enable kdump.service",
                "--write",
                f"{KDUMP_SYSCTL_PATH}:{KDUMP_SYSCTL_CONTENT}",
                "--run-command",
                KDUMP_FINAL_ACTION_CMD,
            ]
        if ctx.is_cloud_image:
            argv += [
                "--run-command",
                _CLOUD_INIT_MASK,
                "--write",
                f"/etc/machine-id:{_SEED_MACHINE_ID}",  # pragma: allowlist secret
            ]
        argv += debug_image_args(ctx.packages, ctx.cleanup)
        argv += [
            "--ssh-inject",
            f"root:file:{ctx.authorized_key}",
            "--upload",
            f"{ctx.readiness_unit_path}:/etc/systemd/system/{READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {READINESS_MARKER}.service",
            "--run-command",
            _SELINUX_PERMISSIVE_SED,
        ]
        return argv

    def normalize(self, qcow2: Path) -> None:
        """Normalize fstab/crypttab/SELinux and force a first-boot SELinux relabel via guestfish.

        The tar->ext4 repack drops SELinux xattrs, so ``/.autorelabel`` forces a first-boot
        ``restorecon``; combined with SELINUX=permissive the guest boots and relabels rather than
        denying the host-written authorized_keys.
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
            "touch /.autorelabel\n"
        )
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
