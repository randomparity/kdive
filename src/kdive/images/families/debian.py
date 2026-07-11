"""The debian-family (apt + kdump-tools) rootfs FamilyCustomizer (ADR-0251, #824).

Encodes the virt-customize argv that turns a Debian genericcloud base into a kdive-ready rootfs.
Debian diverges from ``rhel`` in ways that need a distinct family (all verified against the Debian
package database / manpages, 2026-06-26): apt package names (``kdump-tools``, ``python3-drgn``,
``crash``); ``kdump-tools.service`` not ``kdump.service``; ``ssh.service`` not ``sshd.service``;
AppArmor instead of SELinux (profile-based, so the repack needs no relabel and there is no
``/etc/selinux/config`` to touch). It also enables cloud-init via a baked NoCloud seed (ADR-0288),
the uniform rootfs first-boot mechanism: cloud-init's ssh module generates the sshd host keys
Debian genericcloud ships without, so there is no need for a distro-specific keygen unit.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.catalog.images import Capability
from kdive.images.families._fedora_customize import (
    FSTAB,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    cloud_init_first_boot_args,
    drgn_helper_args,
    makedumpfile_version_marker_args,
)
from kdive.images.families.base import CustomizeContext, _mac_tag
from kdive.images.planes._build_common import run_guestfs_tool
from kdive.images.rootfs_kinds import RootfsImageKind

# Debian debug/guest rootfs: the in-target crash + introspection toolchain by apt name. ``drgn``
# ships as ``python3-drgn`` (which provides ``/usr/bin/drgn``, so the ``kdive-drgn`` helper's
# ``drgn -k`` works); ``kdump-tools`` provides kdump (it pulls ``kexec-tools``; ``makedumpfile`` is
# only a Recommends, so it is named explicitly to guarantee it on a minimal base).
_DEBIAN_DEBUG_PACKAGES = ("makedumpfile", "kdump-tools", "crash", "python3-drgn", "openssh-server")
# A build-host toolchain image: the kernel-build deps by Debian package name (``-dev`` not Fedora's
# ``-devel``; ``dwarves`` provides ``pahole`` for BTF as on Fedora).
_DEBIAN_BUILD_PACKAGES = (
    "gcc",
    "make",
    "bc",
    "bison",
    "flex",
    "libssl-dev",
    "libelf-dev",
    "libncurses-dev",
    "dwarves",
    "rsync",
    "git",
)

# kdump-tools.service no-ops unless ``USE_KDUMP=1`` in /etc/default/kdump-tools; the default ships
# disabled. Strip any existing (commented or not) ``USE_KDUMP`` line, then append exactly one set to
# 1 — the same strip-then-append idiom the rhel ``final_action`` pin uses so the file carries one.
_USE_KDUMP_CMD = (
    "sed -i '/^[[:space:]]*#\\?[[:space:]]*USE_KDUMP[[:space:]]*=/d' /etc/default/kdump-tools && "
    "printf 'USE_KDUMP=1\\n' >> /etc/default/kdump-tools"
)
_GUESTFISH_TIMEOUT_S = 5 * 60

type RunGuestfs = Callable[..., None]


class DebianFamily:
    """The debian-family (apt + kdump-tools) :class:`FamilyCustomizer`."""

    family = "debian"
    kdump_unit = "kdump-tools.service"
    guest_mac = "apparmor"

    def packages(self, kind: RootfsImageKind, distro: str, version: str) -> tuple[str, ...]:
        """Return the apt package set for ``kind`` (``distro``/``version`` reserved for parity)."""
        del distro, version
        if kind == "build":
            return _DEBIAN_BUILD_PACKAGES
        return _DEBIAN_DEBUG_PACKAGES

    def capabilities(
        self, kind: RootfsImageKind, distro: str, version: str
    ) -> tuple[Capability, ...]:
        """Return the tags this family bakes (distro/version unused, kept for parity)."""
        del distro, version
        mac = _mac_tag(self.guest_mac)
        if kind == "build":
            return (mac, Capability.BUILD)
        return (Capability.SSH, mac, Capability.KDUMP, Capability.DRGN)

    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Build the virt-customize argv that turns the Debian base into a kdive-ready rootfs."""
        argv: list[str] = ["--install", ",".join(ctx.packages)]
        # Enable ssh exactly when this image declares the SSH capability, which ``capabilities()``
        # ties to ``kind`` (every debug image, never a build-host image). Gating on ``kind`` (not
        # package membership) keeps the declaration and the enable from diverging: a debug image
        # that somehow lacks openssh-server fails the build loudly here rather than shipping an
        # ``ssh``-tagged image with no sshd.
        if ctx.kind == "debug":
            argv += ["--run-command", "systemctl enable ssh.service"]
        # Gate kdump enable + the NMI-panic sysctl on the kdump package (in every debug set, absent
        # from the build set) so a build-host image never panics on a stray NMI.
        if "kdump-tools" in ctx.packages:
            argv += [
                "--run-command",
                "systemctl enable kdump-tools.service",
                "--run-command",
                _USE_KDUMP_CMD,
                "--write",
                f"{KDUMP_SYSCTL_PATH}:{KDUMP_SYSCTL_CONTENT}",
            ]
        argv += cloud_init_first_boot_args(ctx)  # cloud-init owns network + host keys now
        # The debug image carries the reviewed kdive-drgn helper (the live introspection contract);
        # Debian needs no NetworkManager keyfile — cloud-init's baked cloud.cfg dhcp4/NoCloud config
        # (above, ADR-0288) DHCPs the NIC on first boot.
        if ctx.kind == "debug":
            argv += drgn_helper_args()
            argv += makedumpfile_version_marker_args()
        argv += [
            "--upload",
            f"{ctx.readiness_unit_path}:/etc/systemd/system/{READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {READINESS_MARKER}.service",
        ]
        return argv

    def normalize(self, qcow2: Path, *, _run_guestfs: RunGuestfs = run_guestfs_tool) -> None:
        """Normalize fstab to a lone ``/`` and drop crypttab via guestfish (#824).

        Unlike the rhel family there is no SELinux relabel: Debian's AppArmor is profile-based
        (loaded from ``/etc/apparmor.d/`` at boot, not from xattrs the tar->ext4 repack strips), its
        default policy leaves sshd unconfined so the injected authorized_keys is not blocked, and
        the genericcloud base ships no ``/etc/selinux/config`` to edit.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fstab_handle:
            fstab_handle.write(FSTAB)
            fstab_path = Path(fstab_handle.name)
        script = f"upload {fstab_path} /etc/fstab\nrm-f /etc/crypttab\n"
        try:
            _run_guestfs(
                ["guestfish", "--rw", "-a", str(qcow2), "-i"],
                stage="guestfish",
                timeout_s=_GUESTFISH_TIMEOUT_S,
                missing_message="guestfish is not installed; cannot normalize the rootfs image",
                failure_message="guestfish normalization failed",
                input_text=script,
            )
        finally:
            fstab_path.unlink(missing_ok=True)
