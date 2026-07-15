"""Fedora/rhel-family rootfs customization primitives (ADR-0251, ADR-0345).

Single source of truth for the constants and typed customization ``Step``s the local-libvirt
rootfs build shares with the :mod:`kdive.images.families.rhel` FamilyCustomizer: the kdive-ready
serial readiness unit, the kdump NMI-panic sysctl and ``final_action`` pin, the debug-image drgn
staging, the shared cloud-init first-boot seed (ADR-0288), and the default debug/build package
sets. Each primitive returns ``list[Step]`` — one source of truth the argv renderer
(``virt_customize`` path) and the firstboot renderer (boot path) both consume (ADR-0345).
"""

from __future__ import annotations

from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families.base import CustomizeContext
from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.images.planes.provenance_probes import (
    DRGN_MARKER_GUEST_PATH,
    MAKEDUMPFILE_MARKER_GUEST_PATH,
)

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

# The authoritative kdive first-boot config. cloud-init's *system config* network setting
# outranks the datasource, so carrying the DHCP config here (not only in the seed) defeats a base
# image that ships `network: {config: disabled}`. `mode: "off"` is quoted — unquoted `off` is a
# YAML boolean. `match: {name: "e*"}` is interface-name-independent under the SLIRP NIC.
# `growpart` stays off — the rootfs is a no-partition-table whole-disk ext4 (ADR-0030), so there
# is no partition to grow. `resize_rootfs` is on so cloud-init's cc_resizefs grows that whole-disk
# ext4 to fill an overlay sized larger than the base at provision (ADR-0312, #985).
KDIVE_CLOUD_CFG_PATH = "/etc/cloud/cloud.cfg.d/99-kdive.cfg"
KDIVE_CLOUD_CFG_CONTENT = """\
datasource_list: [ NoCloud ]
disable_root: false
network:
  version: 2
  ethernets:
    kdive-dhcp:
      match: { name: "e*" }
      dhcp4: true
      dhcp-identifier: mac
growpart: { mode: "off" }
resize_rootfs: true
"""
NOCLOUD_SEED_DIR = "/var/lib/cloud/seed/nocloud"
_NOCLOUD_META_DATA = "instance-id: kdive-rootfs\nlocal-hostname: kdive\n"
# The body MUST yaml-parse to a mapping, not None. cloud-init 24.4's `_should_wait_via_user_data`
# does `"write_files" in yaml.safe_load(user_data)` with no None guard; a bare `#cloud-config\n`
# parses to None and crashes it (`argument of type 'NoneType' is not iterable`), failing the
# init-local stage → no network → the customization boot's dnf can never reach a mirror. The `{}`
# keeps a valid, empty cloud-config that parses to a dict. EL9's cloud-init 24.4-8 has this bug;
# Fedora's build guards None, so #1147 never hit it (#1152, ADR-0288).
_NOCLOUD_USER_DATA = "#cloud-config\n{}\n"
# Best-effort strip of any base drop-in that disables cloud-init network management; the build
# self-check (rootfs_build.py) is the guard that asserts none remain.
_STRIP_NET_DISABLE_CMD = (
    "for f in /etc/cloud/cloud.cfg.d/*.cfg; do "
    '[ -e "$f" ] || continue; '
    "grep -qs 'config:[[:space:]]*disabled' \"$f\" && grep -qs 'network' \"$f\" "
    '&& rm -f "$f"; done; true'
)


def cloud_init_first_boot_steps(ctx: CustomizeContext) -> list[Step]:
    """Steps that make cloud-init the uniform first-boot mechanism (ADR-0288, ADR-0345).

    Bakes the authoritative kdive ``cloud.cfg.d`` drop-in (network + NoCloud pin + root
    protection) and a NoCloud seed, strips any base network-disabling drop-in, undoes any
    cloud-init disable, and seeds ``machine-id``. Family-neutral. Installs cloud-init on a
    non-cloud (virt-builder) base, which ships none.

    It does **not** ``systemctl enable`` specific cloud-init units: the vendor cloud bases ship
    the cloud-init units already enabled, and ``--install cloud-init`` enables them via the
    package systemd preset on the virt-builder base. Enumerating unit names would break across
    cloud-init versions (24.x renamed ``cloud-init.service`` to ``cloud-init-network.service``,
    live-found on Debian 13); leaving enablement to the vendor/package is version-robust.

    Args:
        ctx: The customize context; ``is_cloud_image`` gates the cloud-init install.
    """
    steps: list[Step] = []
    if not ctx.is_cloud_image:
        steps.append(InstallPackages(("cloud-init",)))
    steps.append(Mkdir(NOCLOUD_SEED_DIR))
    steps.append(StageFile(KDIVE_CLOUD_CFG_PATH, KDIVE_CLOUD_CFG_CONTENT))
    steps.append(StageFile(f"{NOCLOUD_SEED_DIR}/meta-data", _NOCLOUD_META_DATA))
    steps.append(StageFile(f"{NOCLOUD_SEED_DIR}/user-data", _NOCLOUD_USER_DATA))
    steps.append(RunCommand(_STRIP_NET_DISABLE_CMD))
    steps.append(RunCommand("rm -f /etc/cloud/cloud-init.disabled"))
    steps.append(WriteFile("/etc/machine-id", SEED_MACHINE_ID))  # pragma: allowlist secret
    return steps


def readiness_unit(kdump_unit: str, console_device: str) -> str:
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

    ``After=network-online.target`` (+ ``Wants=``) makes ``ready`` also imply the NIC obtained its
    cloud-init DHCP lease (ADR-0288): without it the serial ``ready`` marker fires the same instant
    the network comes up, so an ``authorize_ssh_key`` at ``ready`` races the lease and fails
    ``transport_failure`` (live-found on Debian 13, where cloud-init.target and the marker landed in
    the same second). local-libvirt renders exactly one NIC under SLIRP, which always leases, so
    ``systemd-networkd-wait-online`` cannot stall on an un-leased link.

    Args:
        kdump_unit: The family's kdump systemd unit (``kdump.service`` on ``rhel``,
            ``kdump-tools.service`` on ``debian``); a wrong/absent name silently reopens the race
            (#824).
        console_device: The arch-resolved serial console device (``ttyS0`` on x86, ``hvc0`` on
            pseries — see ``kdive.domain.platform``). The unit orders after ``dev-<device>.device``
            and echoes the marker to ``/dev/<device>``; on pseries a ``ttyS0`` unit would order
            after a device that never appears and write to a console that does not exist, so the
            marker would never reach the host serial log and provisioning would time out.
    """
    return f"""[Unit]
Description=Signal kdive serial readiness
After=dev-{console_device}.device {kdump_unit} network-online.target
Wants=dev-{console_device}.device network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo {READINESS_MARKER} > /dev/{console_device}'
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


def drgn_helper_source() -> Path:
    """Resolve the reviewed ``kdive-drgn`` reference helper from the source tree (ADR-0220)."""
    return Path(__file__).parents[4].joinpath(*DRGN_HELPER_REPO_RELPATH)


def drgn_helper_steps() -> list[Step]:
    """Stage the reviewed ``kdive-drgn`` in-guest helper, read-executable (ADR-0220, #724).

    Returns the step that uploads the helper and makes it ``0755``. Family-neutral: the live
    ``introspect.run`` path SSH-execs this fixed program on any debug guest carrying a working
    ``drgn`` (``drgn`` on ``rhel``, ``python3-drgn`` — which ships ``/usr/bin/drgn`` — on
    ``debian``, #824).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the reviewed ``kdive-drgn`` helper is not a
            readable file in the source tree — fail loud (before returning the step) rather than
            defer a missing helper to guestfish runtime and ship a guest that cannot introspect.
    """
    helper = drgn_helper_source()
    if not helper.is_file():
        raise CategorizedError(
            "the kdive-drgn in-guest helper is missing from the source tree; cannot build a "
            "debug rootfs that can be live-introspected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"helper": str(helper)},
        )
    return [UploadFile(helper, DRGN_HELPER_GUEST_PATH, mode="0755")]


def makedumpfile_version_marker_steps() -> list[Step]:
    """Step recording ``makedumpfile --version`` to a guest marker file.

    Read back at build time into ``provenance["makedumpfile_version"]`` (ADR-0253), the per-image
    operand of the computed kdump-capability predicate. ``makedumpfile -v`` prints
    ``makedumpfile: version X.Y.Z (released ...)`` (there is no ``--version`` long option).
    Best-effort: the command never fails the build (``|| true``); an image without makedumpfile (or
    with it off ``PATH``) leaves an empty marker, which the probe treats as "absent". ``PATH`` is
    tried first, then the canonical ``/usr/sbin`` location, so a run-command shell with a thin
    ``PATH`` still populates the marker.
    """
    return [
        RunCommand(
            "mkdir -p /usr/lib/kdive && "
            "{ command -v makedumpfile >/dev/null 2>&1 && makedumpfile -v "
            "|| /usr/sbin/makedumpfile -v ; } "
            f"> {MAKEDUMPFILE_MARKER_GUEST_PATH} 2>/dev/null || true"
        )
    ]


def drgn_version_marker_steps() -> list[Step]:
    """Step recording ``drgn --version`` to a guest marker file.

    Read back at build time into ``provenance["drgn_version"]`` (ADR-0334), the per-image operand
    of the computed ``live_drgn`` capability predicate (ADR-0328) — the same marker/probe pipeline
    ``makedumpfile_version`` uses. ``drgn --version`` prints ``drgn X.Y.Z``. Family-neutral: the
    drgn CLI lives at ``/usr/bin/drgn`` on every debug image (``drgn`` on rhel, ``python3-drgn`` on
    debian). Best-effort: the command never fails the build (``|| true``); an image without drgn
    (or with it off ``PATH``) leaves an empty marker, which the probe treats as "absent". ``PATH``
    is tried first, then the canonical ``/usr/bin`` location, so a run-command shell with a thin
    ``PATH`` still populates the marker.
    """
    return [
        RunCommand(
            "mkdir -p /usr/lib/kdive && "
            "{ command -v drgn >/dev/null 2>&1 && drgn --version "
            "|| /usr/bin/drgn --version ; } "
            f"> {DRGN_MARKER_GUEST_PATH} 2>/dev/null || true"
        )
    ]


def debug_image_steps(packages: tuple[str, ...]) -> list[Step]:
    """Stage the drgn helper for an ``rhel`` debug image (ADR-0220, #724).

    Non-debug images (no ``drgn`` in ``packages``) get no steps.
    """
    if "drgn" not in packages:
        return []
    return drgn_helper_steps()
