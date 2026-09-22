"""The SUSE-family rootfs customizer for openSUSE debug images (#825, ADR-0251)."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families._fedora_customize import (
    FSTAB,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    cloud_init_first_boot_steps,
    drgn_helper_steps,
    drgn_version_marker_steps,
    makedumpfile_version_marker_steps,
)
from kdive.images.families.base import CustomizeContext, _mac_tag
from kdive.images.families.steps import (
    InstallPackages,
    RunCommand,
    StageFile,
    Step,
    UploadFile,
    WriteFile,
)
from kdive.images.planes._build_common import run_guestfs_tool
from kdive.images.rootfs.kinds import RootfsImageKind
from kdive.providers.shared.build_timeouts import appliance_budget_s

_SUPPORTED_DISTROS = frozenset({"opensuse-tumbleweed", "opensuse-leap"})
_COMMON_DEBUG_PACKAGES = (
    "kdump",
    "kexec-tools",
    "makedumpfile",
    "dracut",
    "crash",
    "openssh-server",
)
_ZYPPER_REFRESH_CMD = "zypper --non-interactive refresh"
_KDUMP_POST_PATH = "/usr/local/sbin/kdive-suse-kdump-post"
_KDUMP_DUMP_ROOT = "/kdump/mnt/var/crash"
_KDUMP_POST_PROGRAMS = ("/usr/bin/mv", "/usr/bin/sync", "/usr/bin/umount", "/usr/sbin/poweroff")
_KDUMP_POST_CONTENT = f"""\
#!/bin/sh
set -eu

dump_root={_KDUMP_DUMP_ROOT}
candidate_count=0
successful_dir=

for dump_dir in "$dump_root"/*; do
    [ -d "$dump_dir" ] || continue
    [ -f "$dump_dir/vmcore" ] || continue
    candidate_count=$((candidate_count + 1))
    if [ -f "$dump_dir/README.txt" ]; then
        while IFS= read -r line; do
            if [ "$line" = "vmcore status: saved successfully" ]; then
                successful_dir=$dump_dir
                break
            fi
        done < "$dump_dir/README.txt"
    fi
done

if [ "$candidate_count" -ne 1 ] || [ -z "$successful_dir" ]; then
    for dump_dir in "$dump_root"/*; do
        [ -f "$dump_dir/vmcore" ] || continue
        /usr/bin/mv -f -- "$dump_dir/vmcore" "$dump_dir/vmcore-incomplete"
    done
fi

/usr/bin/sync
/usr/bin/umount -a || true
/usr/sbin/poweroff -f
"""
_KDUMP_CONFIG_CMD = (
    "sed -i '/^[[:space:]]*#\\?[[:space:]]*KDUMP_\\(REQUIRED_PROGRAMS\\|POSTSCRIPT\\)"
    "[[:space:]]*=/d' /etc/sysconfig/kdump && "
    "printf '%s\\n' "
    f"'KDUMP_REQUIRED_PROGRAMS=\"$KDUMP_REQUIRED_PROGRAMS {_KDUMP_POST_PATH} "
    f"{' '.join(_KDUMP_POST_PROGRAMS)}\"' "
    f"'KDUMP_POSTSCRIPT=\"{_KDUMP_POST_PATH}\"' >> /etc/sysconfig/kdump"
)
_GUESTFISH_TIMEOUT_S = 5 * 60

type RunGuestfs = Callable[..., str]


def _validate(kind: RootfsImageKind, distro: str) -> None:
    if kind != "debug":
        raise CategorizedError(
            "SUSE rootfs images support only the debug kind",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "kind", "kind": kind},
        )
    if distro not in _SUPPORTED_DISTROS:
        raise CategorizedError(
            f"unsupported SUSE rootfs distro: {distro}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "distro", "distro": distro},
        )


class SuseFamily:
    """The debug-only zypper/AppArmor family for Tumbleweed and Leap 15.6."""

    family = "suse"
    kdump_unit = "kdump.service"
    guest_mac = "apparmor"
    install_command = "zypper --non-interactive install --no-recommends"

    def packages(self, kind: RootfsImageKind, distro: str, version: str) -> tuple[str, ...]:
        del version
        _validate(kind, distro)
        if distro == "opensuse-tumbleweed":
            return (*_COMMON_DEBUG_PACKAGES, "drgn")
        return _COMMON_DEBUG_PACKAGES

    def capabilities(
        self, kind: RootfsImageKind, distro: str, version: str
    ) -> tuple[Capability, ...]:
        del version
        _validate(kind, distro)
        capabilities = (Capability.SSH, _mac_tag(self.guest_mac), Capability.KDUMP)
        if distro == "opensuse-tumbleweed":
            return (*capabilities, Capability.DRGN)
        return capabilities

    def customize_steps(self, ctx: CustomizeContext) -> list[Step]:
        """Build the ordered steps that turn an openSUSE cloud base into a debug rootfs."""
        _validate(ctx.kind, ctx.distro)
        steps: list[Step] = [RunCommand(_ZYPPER_REFRESH_CMD), InstallPackages(ctx.packages)]
        steps += [
            RunCommand("systemctl enable sshd.service"),
            RunCommand("systemctl enable kdump.service"),
            WriteFile(KDUMP_SYSCTL_PATH, KDUMP_SYSCTL_CONTENT),
            StageFile(_KDUMP_POST_PATH, _KDUMP_POST_CONTENT),
            RunCommand(f"chmod 0755 {_KDUMP_POST_PATH}"),
            RunCommand(_KDUMP_CONFIG_CMD),
        ]
        steps += cloud_init_first_boot_steps(ctx)
        if "drgn" in ctx.packages:
            steps += drgn_helper_steps()
            steps += drgn_version_marker_steps()
        steps += makedumpfile_version_marker_steps()
        steps.append(
            UploadFile(ctx.readiness_unit_path, f"/etc/systemd/system/{READINESS_MARKER}.service")
        )
        steps.append(RunCommand(f"systemctl enable {READINESS_MARKER}.service"))
        return steps

    def normalize(self, qcow2: Path, *, _run_guestfs: RunGuestfs = run_guestfs_tool) -> None:
        """Normalize the whole-disk ext4 layout without SELinux-specific mutations."""
        with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fstab_handle:
            fstab_handle.write(FSTAB)
            fstab_path = Path(fstab_handle.name)
        script = f"upload {fstab_path} /etc/fstab\nrm-f /etc/crypttab\n"
        try:
            _run_guestfs(
                ["guestfish", "--rw", "-a", str(qcow2), "-i"],
                stage="guestfish",
                timeout_s=appliance_budget_s(_GUESTFISH_TIMEOUT_S),
                missing_message="guestfish is not installed; cannot normalize the rootfs image",
                failure_message="guestfish normalization failed",
                input_text=script,
            )
        finally:
            fstab_path.unlink(missing_ok=True)
