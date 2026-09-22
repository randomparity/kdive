"""The SUSE-family rootfs customizer for openSUSE debug images (#825, ADR-0251)."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from compression import zstd
from pathlib import Path

from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families._fedora_customize import (
    FSTAB,
    KDIVE_CLOUD_CFG_CONTENT,
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
from kdive.images.rootfs.initrd import remove_zstd_newc_entry
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
# Both SUSE cloud-init renderers treat the v2 mapping key as an interface name. Keep the shared
# wildcard match, but name the predictable QEMU interface so NetworkManager/Wicked configure it.
_SUSE_CLOUD_CFG_CONTENT = KDIVE_CLOUD_CFG_CONTENT.replace("    kdive-dhcp:\n", "    eth0:\n")
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
        saved=0
        unsupported=0
        while IFS= read -r line; do
            [ "$line" = "vmcore status: saved successfully" ] && saved=1
            case "$line" in
                "The kernel version is not supported."|\
                "The makedumpfile operation may be incomplete.") unsupported=1 ;;
            esac
        done < "$dump_dir/README.txt"
        if [ "$saved" -eq 1 ] && [ "$unsupported" -eq 0 ]; then
            successful_dir=$dump_dir
        fi
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
_KIWI_REPART_HOOK = "var/lib/dracut/hooks/pre-mount/20-kiwi-repart-disk.sh"

type RunGuestfs = Callable[..., str]
type RewriteInitrd = Callable[[Path, str], bool]


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
        steps += cloud_init_first_boot_steps(ctx, cloud_cfg_content=_SUSE_CLOUD_CFG_CONTENT)
        if "drgn" in ctx.packages:
            steps += drgn_helper_steps()
            steps += drgn_version_marker_steps()
        steps += makedumpfile_version_marker_steps()
        steps.append(
            UploadFile(ctx.readiness_unit_path, f"/etc/systemd/system/{READINESS_MARKER}.service")
        )
        steps.append(RunCommand(f"systemctl enable {READINESS_MARKER}.service"))
        return steps

    def normalize(
        self,
        qcow2: Path,
        *,
        _run_guestfs: RunGuestfs = run_guestfs_tool,
        _rewrite_initrd: RewriteInitrd = remove_zstd_newc_entry,
    ) -> None:
        """Normalize the ext4 layout and remove a stale KIWI partition-resize hook.

        Tumbleweed's appliance initrd carries a hook for its original partition table. KDIVE
        repacks the image as a partitionless whole-disk filesystem, so retaining that hook makes
        dracut wait for a nonexistent partition before it can mount ``/dev/vda``. Leap lacks the
        hook; the bounded rewrite returns ``False`` and leaves its initrd byte-identical.
        """
        with tempfile.TemporaryDirectory(prefix="kdive-suse-normalize-") as temp_dir:
            temp = Path(temp_dir)
            fstab_path = temp / "fstab"
            initrd_path = temp / "initrd"
            fstab_path.write_text(FSTAB, encoding="utf-8")
            self._normalize_and_download(qcow2, fstab_path, initrd_path, _run_guestfs)
            try:
                rewritten = _rewrite_initrd(initrd_path, _KIWI_REPART_HOOK)
            except (OSError, ValueError, zstd.ZstdError) as exc:
                raise CategorizedError(
                    "SUSE initrd normalization failed",
                    category=ErrorCategory.PROVISIONING_FAILURE,
                    details={"error": type(exc).__name__},
                ) from exc
            if rewritten:
                self._upload_initrd(qcow2, initrd_path, _run_guestfs)

    @staticmethod
    def _normalize_and_download(
        qcow2: Path, fstab_path: Path, initrd_path: Path, run_guestfs: RunGuestfs
    ) -> None:
        script = (
            f"upload {fstab_path} /etc/fstab\n"
            "rm-f /etc/crypttab\n"
            f"download /boot/initrd {initrd_path}\n"
        )
        run_guestfs(
            ["guestfish", "--rw", "-a", str(qcow2), "-i"],
            stage="guestfish",
            timeout_s=appliance_budget_s(_GUESTFISH_TIMEOUT_S),
            missing_message="guestfish is not installed; cannot normalize the rootfs image",
            failure_message="guestfish normalization failed",
            input_text=script,
        )

    @staticmethod
    def _upload_initrd(qcow2: Path, initrd_path: Path, run_guestfs: RunGuestfs) -> None:
        run_guestfs(
            ["guestfish", "--rw", "-a", str(qcow2), "-i"],
            stage="guestfish",
            timeout_s=appliance_budget_s(_GUESTFISH_TIMEOUT_S),
            missing_message="guestfish is not installed; cannot normalize the rootfs image",
            failure_message="guestfish initrd upload failed",
            input_text=f"upload {initrd_path} /boot/initrd\n",
        )
