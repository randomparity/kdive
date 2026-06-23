"""The in-process local-libvirt rootfs build plane (M2.4/2, ADR-0052, ADR-0092).

`LocalLibvirtRootfsBuildPlane` orchestrates the same unprivileged libguestfs stages the deleted
bash rootfs builder ran, but in-process and with **pinned-input provenance** recorded into the
:class:`RootfsBuildOutput`:

1. resolve the kdive-managed SSH public key (ADR-0052 — the single source of truth shared with
   the connect-time ``ssh -i`` identity);
2. ``virt-builder`` customizes a base scratch image: install ``openssh-server`` + the spec's
   packages, enable ``sshd``, inject the authorized key, and install a ``kdive-ready`` oneshot
   unit that echoes the readiness marker to ``/dev/ttyS0`` on boot;
3. ``virt-tar-out`` + ``virt-make-fs --type=ext4 --format=qcow2`` repack the root tree into a
   **no-partition-table whole-disk ext4 qcow2** — the only layout the direct-kernel boot
   provider mounts (``root=/dev/vda``, no initramfs, ADR-0030);
4. ``guestfish`` normalizes the inherited mount config to a lone ``/`` fstab entry, removes
   ``/etc/crypttab``, and disables guest-internal SELinux (so the host-written authorized_keys is
   read without a relabel and the first boot does not relabel+reboot).

The slow libguestfs tools are **injected seams** (:class:`RootfsBuildTools`) that default to the
real implementations, so unit tests cover the orchestration/provenance contract without
libguestfs or qemu; the real path is exercised on the operator-run live-stack path. ``build()``
is synchronous — the worker offloads the whole call via ``asyncio.to_thread`` (ADR-0092).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.distros import resolve_base_template
from kdive.images.planes._build_common import (
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.prereqs.managed_ssh_key import (
    ManagedKeyError,
    ensure_managed_keypair,
    managed_public_key_path,
)
from kdive.providers.shared.build_timeouts import SLOW_BUILD_TOOL_TIMEOUT_S

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_DEFAULT_IMAGE_SIZE = "6G"
_READINESS_MARKER = "kdive-ready"
_VIRT_BUILDER_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_REPACK_TIMEOUT_S = SLOW_BUILD_TOOL_TIMEOUT_S
_GUESTFISH_TIMEOUT_S = 5 * 60

_READINESS_UNIT = f"""[Unit]
Description=Signal kdive serial readiness
After=dev-ttyS0.device
Wants=dev-ttyS0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo {_READINESS_MARKER} > /dev/ttyS0'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
_FSTAB = "/dev/vda / ext4 defaults 0 1\n"
_SELINUX_CONFIG = "SELINUX=disabled\nSELINUXTYPE=targeted\n"
# Local `control.force_crash` injects an NMI; the guest must panic on it for kdump to trigger.
# Staged only on the kdump image, the local equivalent of the remote base-image obligation
# (ADR-0213, #688, mirrors ADR-0084).
_KDUMP_SYSCTL_PATH = "/etc/sysctl.d/99-kdive-kdump.conf"
_KDUMP_SYSCTL_CONTENT = "kernel.unknown_nmi_panic=1\n"
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
_KDUMP_FINAL_ACTION_CMD = (
    "sed -i '/^[[:space:]]*final_action[[:space:]]/d' /etc/kdump.conf && "
    "printf 'final_action poweroff\\n' >> /etc/kdump.conf"
)
# The live `introspect.run` path (ADR-0219) SSH-execs this fixed-argv in-guest helper; the debug
# image must carry the repo's reviewed reference implementation, made read-executable. `build-fs`
# runs `python -m kdive` from the source checkout, so the helper resolves relative to the source
# tree. Staged only on the debug image (`drgn` in packages) (ADR-0220, #724).
_DRGN_HELPER_GUEST_PATH = "/usr/local/sbin/kdive-drgn"
_DRGN_HELPER_REPO_RELPATH = ("deploy", "remote-libvirt-guest-helpers", "kdive-drgn")
# The drgn-live SSH transport (ADR-0218) renders a SLIRP NIC the guest must DHCP to be reachable.
# An interface-name-independent NetworkManager keyfile DHCPs whatever ethernet device the SSH NIC
# enumerates as under direct-kernel boot (no stable NIC naming). Written 0600 — NM ignores a
# world-readable keyfile (ADR-0220, #724).
_SSH_NIC_KEYFILE_PATH = "/etc/NetworkManager/system-connections/kdive-ssh-nic.nmconnection"
_SSH_NIC_KEYFILE_CONTENT = """[connection]
id=kdive-ssh-nic
type=ethernet
autoconnect=true
autoconnect-priority=-100

[ipv4]
method=auto

[ipv6]
method=ignore
"""


def _drgn_helper_source() -> Path:
    """Resolve the reviewed ``kdive-drgn`` reference helper from the source tree (ADR-0220)."""
    return Path(__file__).parents[4].joinpath(*_DRGN_HELPER_REPO_RELPATH)


def _resolve_managed_public_key() -> Path:
    """Resolve the kdive-managed SSH public key, generating the keypair if absent (ADR-0052)."""
    try:
        ensure_managed_keypair()
        return managed_public_key_path()
    except ManagedKeyError as exc:
        raise CategorizedError(
            "could not resolve the kdive-managed SSH public key to install into the rootfs",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"error": type(exc).__name__},
        ) from exc


def _run(argv: list[str], *, stage: str, timeout_s: int) -> None:
    """Run a fixed-argv libguestfs tool, mapping failure onto a categorized error."""
    run_guestfs_tool(
        argv,
        stage=stage,
        timeout_s=timeout_s,
        missing_message=f"{argv[0]} is not installed; cannot build the rootfs image",
    )


def _debug_image_args(packages: tuple[str, ...], cleanup: list[Path]) -> list[str]:
    """Stage the drgn helper + SSH-NIC DHCP keyfile for a debug image (ADR-0220, #724).

    Returns the virt-builder argv fragment and appends any tempfiles to ``cleanup`` for the
    caller to unlink. Non-debug images (no ``drgn`` in ``packages``) get an empty fragment.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the reviewed ``kdive-drgn`` helper is not a
            readable file in the source tree — fail loud rather than ship a guest that cannot
            introspect.
    """
    if "drgn" not in packages:
        return []
    helper = _drgn_helper_source()
    if not helper.is_file():
        raise CategorizedError(
            "the kdive-drgn in-guest helper is missing from the source tree; cannot build a "
            "debug rootfs that can be live-introspected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"helper": str(helper)},
        )
    with tempfile.NamedTemporaryFile("w", suffix=".nmconnection", delete=False) as keyfile:
        keyfile.write(_SSH_NIC_KEYFILE_CONTENT)
        keyfile_path = Path(keyfile.name)
    cleanup.append(keyfile_path)
    return [
        "--upload",
        f"{helper}:{_DRGN_HELPER_GUEST_PATH}",
        "--run-command",
        f"chmod 0755 {_DRGN_HELPER_GUEST_PATH}",
        "--upload",
        f"{keyfile_path}:{_SSH_NIC_KEYFILE_PATH}",
        "--run-command",
        f"chmod 0600 {_SSH_NIC_KEYFILE_PATH}",
    ]


def _real_virt_builder(
    *,
    distro: str,
    releasever: str,
    packages: tuple[str, ...],
    authorized_key: Path,
    scratch: Path,
    size: str,
) -> None:
    """Customize a base scratch image: sshd + key + the kdive-ready marker unit + packages."""
    template = resolve_base_template(distro, releasever)
    cleanup: list[Path] = []
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as unit:
        unit.write(_READINESS_UNIT)
        unit_path = Path(unit.name)
    cleanup.append(unit_path)
    try:
        argv = [
            "virt-builder",
            template,
            "--format",
            "qcow2",
            "--size",
            size,
            "--output",
            str(scratch),
            "--install",
            "openssh-server",
            "--run-command",
            "systemctl enable sshd.service",
        ]
        if packages:
            argv += ["--install", ",".join(packages)]
        if "kdump-utils" in packages:
            argv += [
                "--run-command",
                "systemctl enable kdump.service",
                "--write",
                f"{_KDUMP_SYSCTL_PATH}:{_KDUMP_SYSCTL_CONTENT}",
                "--run-command",
                _KDUMP_FINAL_ACTION_CMD,
            ]
        argv += _debug_image_args(packages, cleanup)
        argv += [
            "--ssh-inject",
            f"root:file:{authorized_key}",
            "--upload",
            f"{unit_path}:/etc/systemd/system/{_READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {_READINESS_MARKER}.service",
        ]
        _run(argv, stage="virt-builder", timeout_s=_VIRT_BUILDER_TIMEOUT_S)
    finally:
        for path in cleanup:
            path.unlink(missing_ok=True)


def _real_repack_whole_disk_ext4(*, scratch: Path, qcow2: Path, size: str) -> None:
    """Repack the customized root tree into a no-partition-table whole-disk ext4 qcow2."""
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        tar_path = Path(handle.name)
    try:
        _run(
            ["virt-tar-out", "-a", str(scratch), "/", str(tar_path)],
            stage="virt-tar-out",
            timeout_s=_REPACK_TIMEOUT_S,
        )
        _run(
            [
                "virt-make-fs",
                "--type=ext4",
                "--format=qcow2",
                f"--size={size}",
                str(tar_path),
                str(qcow2),
            ],
            stage="virt-make-fs",
            timeout_s=_REPACK_TIMEOUT_S,
        )
    finally:
        tar_path.unlink(missing_ok=True)


def _real_normalize_guest(qcow2: Path) -> None:
    """Normalize fstab to a lone ``/``, remove crypttab, and disable guest SELinux via guestfish."""
    with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fstab_handle:
        fstab_handle.write(_FSTAB)
        fstab_path = Path(fstab_handle.name)
    with tempfile.NamedTemporaryFile("w", suffix=".selinux", delete=False) as selinux_handle:
        selinux_handle.write(_SELINUX_CONFIG)
        selinux_path = Path(selinux_handle.name)
    script = (
        f"upload {fstab_path} /etc/fstab\n"
        f"upload {selinux_path} /etc/selinux/config\n"
        "rm-f /etc/crypttab\n"
    )
    try:
        _run_guestfish(qcow2, script)
    finally:
        fstab_path.unlink(missing_ok=True)
        selinux_path.unlink(missing_ok=True)


def _run_guestfish(qcow2: Path, script: str) -> None:
    run_guestfs_tool(
        ["guestfish", "--rw", "-a", str(qcow2), "-i"],
        stage="guestfish",
        timeout_s=_GUESTFISH_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot normalize the rootfs image",
        failure_message="guestfish normalization failed",
        input_text=script,
    )


type ResolveAuthorizedKey = Callable[[], Path]
type VirtBuilder = Callable[..., None]
type RepackWholeDiskExt4 = Callable[..., None]
type NormalizeGuest = Callable[[Path], None]


@dataclass(frozen=True, slots=True)
class RootfsBuildTools:
    """The injectable build seams; default to the real libguestfs implementations."""

    resolve_authorized_key: ResolveAuthorizedKey = _resolve_managed_public_key
    virt_builder: VirtBuilder = _real_virt_builder
    repack_whole_disk_ext4: RepackWholeDiskExt4 = _real_repack_whole_disk_ext4
    normalize_guest: NormalizeGuest = _real_normalize_guest


class LocalLibvirtRootfsBuildPlane:
    """The realized local-libvirt :class:`~kdive.images.planes.base.RootfsBuildPlane`."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        size: str = _DEFAULT_IMAGE_SIZE,
        tools: RootfsBuildTools | None = None,
    ) -> None:
        self._workspace = workspace or Path(_DEFAULT_WORKSPACE)
        self._size = size
        self._tools = tools or RootfsBuildTools()

    @classmethod
    def from_env(cls, *, workspace: Path | None = None) -> LocalLibvirtRootfsBuildPlane:
        """Build with the real libguestfs seams; does not run any tool or touch the network.

        Args:
            workspace: Override the default build/publish workspace (``build-fs --workspace``)
                so an operator can build under a user-writable path without a privileged
                ``mkdir`` of the root-owned default.
        """
        return cls(workspace=workspace)

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the kdive-ready rootfs qcow2 for ``spec``; record pinned-input provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unresolvable authorized key,
                ``MISSING_DEPENDENCY`` for absent libguestfs tooling, or ``PROVISIONING_FAILURE``
                for a build-stage failure.
        """
        validate_image_name(spec.name)
        authorized_key = self._tools.resolve_authorized_key()
        if not authorized_key.is_file():
            raise CategorizedError(
                "resolved SSH public key is not a readable file; cannot build the rootfs image",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"authorized_key": str(authorized_key)},
            )
        with build_workspace(self._workspace, prefix="rootfs-build-") as work_dir:
            scratch = work_dir / "scratch.qcow2"
            self._tools.virt_builder(
                distro=spec.distro,
                releasever=spec.releasever,
                packages=spec.packages,
                authorized_key=authorized_key,
                scratch=scratch,
                size=self._size,
            )
            staged = work_dir / f"{spec.name}.qcow2"
            self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
            self._tools.normalize_guest(staged)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            provenance=_provenance(spec, size=self._size, authorized_key=authorized_key),
        )


def _provenance(spec: RootfsBuildSpec, *, size: str, authorized_key: Path) -> dict[str, object]:
    """Record the pinned inputs and build args that produced the image (falsifiable contract).

    ``source_image_digest`` is the caller-declared base/template pin recorded as requested — the
    plane does not re-fetch and checksum the virt-builder template, so it names what was *asked
    for*, not a plane-verified hash. The image's verifiable identity is the output qcow2 content
    digest (:func:`kdive.images.planes._build_common.digest_file`), per ADR-0092.
    """
    return {
        "plane": "local-libvirt",
        "distro": spec.distro,
        "releasever": spec.releasever,
        "packages": list(spec.packages),
        "source_image_digest": spec.source_image_digest,
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "authorized_key_name": authorized_key.name,
        "readiness_marker": _READINESS_MARKER,
        "layout": "whole-disk-ext4-qcow2",
        "guest_selinux": "disabled",
    }
