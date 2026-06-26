"""Fedora/rhel-family rootfs customization primitives (ADR-0251).

Single source of truth for the constants and argv fragments the local-libvirt rootfs build
shares with the :mod:`kdive.images.families.rhel` FamilyCustomizer: the kdive-ready serial
readiness unit, the kdump NMI-panic sysctl and ``final_action`` pin, the debug-image drgn /
SSH-NIC staging, and the default debug/build package sets. Relocated here from
``providers/local_libvirt/rootfs_build.py`` (which now imports them) so the legacy in-line
builder and the new family customizer encode them once.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

# Today's debug/guest rootfs: the in-target crash + introspection toolchain. ``keyutils`` provides
# ``keyctl``, which Fedora ``kdumpctl`` invokes building the crash environment (ADR-0213, #688).
DEFAULT_DEBUG_FS_PACKAGES = ("drgn", "kexec-tools", "makedumpfile", "kdump-utils", "keyutils")
# A build-host toolchain image: the kernel-build deps a remote/ephemeral build target needs.
DEFAULT_BUILD_FS_PACKAGES = (
    "gcc",
    "make",
    "bc",
    "bison",
    "flex",
    "openssl-devel",
    "elfutils-libelf-devel",
    "ncurses-devel",
    "dwarves",
    "rsync",
    "git",
)

READINESS_MARKER = "kdive-ready"

# A valid 32-hex machine-id seeded into cloud-image bases so the first boot does not run
# ``systemctl preset-all`` (which resets the kdump service to its vendor preset — disabled — on both
# Fedora Cloud and Debian genericcloud, whose machine-id ships uninitialized/empty). Not a secret:
# a fixed build-time identity, intentionally constant and shared across families (#824).
SEED_MACHINE_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"  # pragma: allowlist secret


def readiness_unit(kdump_unit: str) -> str:
    """Render the kdive-ready serial unit ordered ``After=<kdump_unit>`` (#817, #824).

    ``After=<kdump_unit>`` closes the arm-vs-ready race (#817): the family's kdump unit and this
    unit are both ``WantedBy=multi-user.target``, so without an ordering edge the serial
    ``kdive-ready`` signal can fire while kdump is still building the capture initramfs + ``kexec
    -p``-loading it. A ``force_crash`` on a System that reported ``ready`` before kdump armed then
    captures nothing (an empty ``/var/crash``). Ordering after the kdump unit makes ``ready`` mean
    "kdump finished its arming attempt"; ``After=`` is pure ordering (no ``Wants=``), so a non-kdump
    build image — where the kdump unit is absent — is unaffected (ordering against an absent unit is
    a no-op), and a kdump that fails to arm still releases readiness (``After=`` releases on the
    unit's terminal state, success or failure), so the System still reaches ``ready`` and a
    force_crash surfaces the capture-time readiness failure instead of provisioning hanging.

    Args:
        kdump_unit: The family's kdump systemd unit (``kdump.service`` on ``rhel``,
            ``kdump-tools.service`` on ``debian``); a wrong/absent name silently reopens the race
            (#824).
    """
    return f"""[Unit]
Description=Signal kdive serial readiness
After=dev-ttyS0.device {kdump_unit}
Wants=dev-ttyS0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo {READINESS_MARKER} > /dev/ttyS0'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""


FSTAB = "/dev/vda / ext4 defaults 0 1\n"

# Local ``control.force_crash`` injects an NMI; the guest must panic on it for kdump to trigger.
# Staged only on the kdump image, the local equivalent of the remote base-image obligation
# (ADR-0213, #688, mirrors ADR-0084).
KDUMP_SYSCTL_PATH = "/etc/sysctl.d/99-kdive-kdump.conf"
KDUMP_SYSCTL_CONTENT = "kernel.unknown_nmi_panic=1\n"
# After dumping, the crash kernel runs kdump's ``final_action``. Pin it to ``poweroff`` so the
# guest self-shuts-off (VIR_DOMAIN_SHUTOFF) the instant the dump completes — the reliable
# completion signal the host-side harvest waits on (ADR-0217). Fedora's default is ``reboot``,
# which never self-shuts-off and would force the harvest onto its bounded-timeout fallback. The
# run-command strips any existing ``final_action`` line, then appends ours, so kdump.conf carries
# exactly one.
#
# ``poweroff`` (NOT ``shutdown``): kdump.conf accepts only ``reboot``/``halt``/``poweroff``; any
# other token makes kdumpctl reject the config (``Starting kdump: [FAILED]``) so kdump never arms
# and no vmcore is written (#705 live regression).
KDUMP_FINAL_ACTION_CMD = (
    "sed -i '/^[[:space:]]*final_action[[:space:]]/d' /etc/kdump.conf && "
    "printf 'final_action poweroff\\n' >> /etc/kdump.conf"
)
# The live ``introspect.run`` path (ADR-0219) SSH-execs this fixed-argv in-guest helper; the debug
# image must carry the repo's reviewed reference implementation, made read-executable. ``build-fs``
# runs ``python -m kdive`` from the source checkout, so the helper resolves relative to the source
# tree. Staged only on the debug image (``drgn`` in packages) (ADR-0220, #724).
DRGN_HELPER_GUEST_PATH = "/usr/local/sbin/kdive-drgn"
DRGN_HELPER_REPO_RELPATH = ("deploy", "remote-libvirt-guest-helpers", "kdive-drgn")
# The drgn-live SSH transport (ADR-0218) renders a SLIRP NIC the guest must DHCP to be reachable.
# An interface-name-independent NetworkManager keyfile DHCPs whatever ethernet device the SSH NIC
# enumerates as under direct-kernel boot (no stable NIC naming). Written 0600 — NM ignores a
# world-readable keyfile (ADR-0220, #724).
SSH_NIC_KEYFILE_PATH = "/etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection"
SSH_NIC_KEYFILE_CONTENT = """[connection]
id=kdive-ssh-nic
type=ethernet
autoconnect=true
autoconnect-priority=-100

[ipv4]
method=auto

[ipv6]
method=ignore
"""


def drgn_helper_source() -> Path:
    """Resolve the reviewed ``kdive-drgn`` reference helper from the source tree (ADR-0220)."""
    return Path(__file__).parents[4].joinpath(*DRGN_HELPER_REPO_RELPATH)


def drgn_helper_args() -> list[str]:
    """Stage the reviewed ``kdive-drgn`` in-guest helper, read-executable (ADR-0220, #724).

    Returns the virt-customize/virt-builder argv fragment that uploads the helper and makes it
    ``0755``. Family-neutral: the live ``introspect.run`` path SSH-execs this fixed program on any
    debug guest carrying a working ``drgn`` (``drgn`` on ``rhel``, ``python3-drgn`` — which ships
    ``/usr/bin/drgn`` — on ``debian``, #824).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the reviewed ``kdive-drgn`` helper is not a
            readable file in the source tree — fail loud rather than ship a guest that cannot
            introspect.
    """
    helper = drgn_helper_source()
    if not helper.is_file():
        raise CategorizedError(
            "the kdive-drgn in-guest helper is missing from the source tree; cannot build a "
            "debug rootfs that can be live-introspected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"helper": str(helper)},
        )
    return [
        "--upload",
        f"{helper}:{DRGN_HELPER_GUEST_PATH}",
        "--run-command",
        f"chmod 0755 {DRGN_HELPER_GUEST_PATH}",
    ]


def _ssh_nic_keyfile_args(cleanup: list[Path]) -> list[str]:
    """Stage the NetworkManager SSH-NIC DHCP keyfile (ADR-0218), 0600 so NM loads it (#724).

    Appends the staged tempfile to ``cleanup`` for the caller to unlink. NetworkManager-specific —
    used by the ``rhel`` family; ``debian`` genericcloud has no NetworkManager and DHCPs the extra
    NIC via cloud-init's ``cloud-ifupdown-helper`` instead (#824).
    """
    with tempfile.NamedTemporaryFile("w", suffix=".nmconnection", delete=False) as keyfile:
        keyfile.write(SSH_NIC_KEYFILE_CONTENT)
        keyfile_path = Path(keyfile.name)
    cleanup.append(keyfile_path)
    return [
        "--upload",
        f"{keyfile_path}:{SSH_NIC_KEYFILE_PATH}",
        "--run-command",
        f"chmod 0600 {SSH_NIC_KEYFILE_PATH}",
    ]


def debug_image_args(packages: tuple[str, ...], cleanup: list[Path]) -> list[str]:
    """Stage the drgn helper + SSH-NIC DHCP keyfile for an ``rhel`` debug image (ADR-0220, #724).

    Returns the virt-customize/virt-builder argv fragment and appends any tempfiles to
    ``cleanup`` for the caller to unlink. Non-debug images (no ``drgn`` in ``packages``) get an
    empty fragment.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the reviewed ``kdive-drgn`` helper is not a
            readable file in the source tree — fail loud rather than ship a guest that cannot
            introspect.
    """
    if "drgn" not in packages:
        return []
    return [*drgn_helper_args(), *_ssh_nic_keyfile_args(cleanup)]
