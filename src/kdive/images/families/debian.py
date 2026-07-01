"""The debian-family (apt + kdump-tools) rootfs FamilyCustomizer (ADR-0251, #824).

Encodes the virt-customize argv that turns a Debian genericcloud base into a kdive-ready rootfs.
Debian diverges from ``rhel`` in ways that need a distinct family (all verified against the Debian
package database / manpages, 2026-06-26): apt package names (``kdump-tools``, ``python3-drgn``,
``crash``); ``kdump-tools.service`` not ``kdump.service``; ``ssh.service`` not ``sshd.service``;
AppArmor instead of SELinux (profile-based, so the repack needs no relabel and there is no
``/etc/selinux/config`` to touch); and — on a cloud-image base — disabling cloud-init with the
version-proof ``/etc/cloud/cloud-init.disabled`` file (Debian 13 renamed ``cloud-init.service`` to
``cloud-init-network.service``, so a fixed unit-mask list would silently miss a stage) plus seeding
``/etc/machine-id`` (empty on genericcloud) so the first-boot ``preset-all`` does not disable
``kdump-tools.service``.
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
    SEED_MACHINE_ID,
    drgn_helper_args,
    makedumpfile_version_marker_args,
)
from kdive.images.families.base import CustomizeContext, _mac_tag
from kdive.images.planes._build_common import run_guestfs_tool

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
# Version-proof cloud-init disable: cloud-init no-ops if this file exists, regardless of the
# per-stage unit names (which Debian 13 renamed) — one write, correct on both 12 and 13.
_CLOUD_INIT_DISABLED_PATH = "/etc/cloud/cloud-init.disabled"

# Debian genericcloud ships openssh-server with NO host keys: cloud-init generates them
# per-instance on first boot. Disabling cloud-init (above) therefore leaves sshd keyless, so
# ``ssh.service`` fails its ``sshd -t`` preflight and rate-limits — SSH (the drgn-live transport)
# never comes up (#824, found by live boot). Debian has no Fedora/RHEL ``sshd-keygen@.service``, so
# stage a oneshot that runs ``ssh-keygen -A`` (creates any missing host-key types) ordered
# ``Before=ssh.service``. The ``ConditionPathExists=!`` gate keeps keys per-instance: it generates
# on a fresh boot but skips a guest that already has keys, so it never overwrites an existing
# identity.
_SSHD_KEYGEN_UNIT_PATH = "/etc/systemd/system/kdive-sshd-keygen.service"
_SSHD_KEYGEN_UNIT = """[Unit]
Description=Generate sshd host keys (kdive)
Before=ssh.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
_GUESTFISH_TIMEOUT_S = 5 * 60

type RunGuestfs = Callable[..., None]


class DebianFamily:
    """The debian-family (apt + kdump-tools) :class:`FamilyCustomizer`."""

    family = "debian"
    kdump_unit = "kdump-tools.service"
    guest_mac = "apparmor"

    def packages(self, kind: str, distro: str, version: str) -> tuple[str, ...]:
        """Return the apt package set for ``kind`` (``distro``/``version`` reserved for parity)."""
        del distro, version
        if kind == "build":
            return _DEBIAN_BUILD_PACKAGES
        return _DEBIAN_DEBUG_PACKAGES

    def capabilities(self, kind: str, distro: str, version: str) -> tuple[Capability, ...]:
        """Return the tags this family bakes (distro/version unused, kept for parity)."""
        del distro, version
        mac = _mac_tag(self.guest_mac)
        if kind == "build":
            return (mac, Capability.BUILD)
        return (Capability.SSH, mac, Capability.KDUMP, Capability.DRGN)

    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Build the virt-customize argv that turns the Debian base into a kdive-ready rootfs."""
        argv: list[str] = [
            "--install",
            ",".join(ctx.packages),
            "--run-command",
            "systemctl enable ssh.service",
            # Generate the sshd host keys cloud-init would have made (see _SSHD_KEYGEN_UNIT).
            "--write",
            f"{_SSHD_KEYGEN_UNIT_PATH}:{_SSHD_KEYGEN_UNIT}",
            "--run-command",
            "systemctl enable kdive-sshd-keygen.service",
        ]
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
        if ctx.is_cloud_image:
            argv += [
                "--touch",
                _CLOUD_INIT_DISABLED_PATH,
                "--write",
                f"/etc/machine-id:{SEED_MACHINE_ID}",  # pragma: allowlist secret
            ]
        # The debug image carries the reviewed kdive-drgn helper (the live introspection contract);
        # Debian needs no NetworkManager keyfile (cloud-init's cloud-ifupdown-helper DHCPs the NIC).
        if ctx.kind == "debug":
            argv += drgn_helper_args()
            argv += makedumpfile_version_marker_args()
        argv += [
            "--ssh-inject",
            f"root:file:{ctx.authorized_key}",
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
