"""Read-only prerequisites for the live VM lane preparation (#2805)."""

from __future__ import annotations

import fcntl
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.providers.shared.libvirt_xml import parse_capabilities_arch, parse_guest_arches

DEFAULT_CPUS = "8"
DEFAULT_MEMORY_MIB = "16384"
DEFAULT_DISK_BYTES = "51539607552"
_MIB = 1024**2
_KIB = 1024


class PreflightError(Exception):
    """An operator can fix a missing or inconsistent lane prerequisite."""


def _local_uri(uri: str) -> None:
    try:
        parsed = urlsplit(uri)
        query = (
            parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
            if parsed.query
            else {}
        )
    except ValueError as exc:
        raise PreflightError(f"invalid local libvirt URI {uri!r}: {exc}") from exc
    if (
        parsed.scheme not in {"qemu", "qemu+unix"}
        or parsed.netloc
        or parsed.path not in {"/session", "/system"}
        or parsed.fragment
        or (parsed.scheme == "qemu" and query)
        or (
            parsed.scheme == "qemu+unix"
            and query
            and (
                set(query) != {"socket"}
                or len(query["socket"]) != 1
                or not Path(query["socket"][0]).is_absolute()
            )
        )
    ):
        raise PreflightError(
            f"{uri!r} is not a local qemu session/system URI; select a local libvirt socket"
        )


def _capabilities(uri: str) -> tuple[str, dict[str, dict[str, str]]]:
    try:
        result = subprocess.run(
            ["virsh", "-c", uri, "capabilities"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(
            f"virsh capabilities failed at {uri}: check the local daemon and URI ({exc})"
        ) from exc
    host = parse_capabilities_arch(result.stdout)
    guests = parse_guest_arches(result.stdout, SUPPORTED_ARCHES)
    if host == "unknown" or not guests:
        raise PreflightError(
            "libvirt capabilities are malformed or lack supported guests; repair libvirt"
        )
    return host, guests


def _check_kvm(host: str, *, power_hv: bool) -> None:
    # PowerPC _IOC_NONE contributes 0x20000000; x86 _IO contributes no direction bits.
    ioctl_base = 0x2000AE00 if host == "ppc64le" else 0xAE00
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        raise PreflightError(f"cannot open /dev/kvm: grant this user KVM access ({exc})") from exc
    vm_fd: int | None = None
    try:
        try:
            if fcntl.ioctl(fd, ioctl_base, 0) != 12:
                raise PreflightError(
                    "KVM_GET_API_VERSION is not 12; use a supported Linux KVM host"
                )
            if power_hv:
                # KVM_VM_PPC_HV=1, supplied to CREATE_VM. An empty VM proves HV is usable.
                vm_fd = fcntl.ioctl(fd, ioctl_base | 1, 1)
        except OSError as exc:
            raise PreflightError(
                f"KVM ioctl failed: enable KVM-HV and grant /dev/kvm access ({exc})"
            ) from exc
    finally:
        try:
            if vm_fd is not None:
                os.close(vm_fd)
        finally:
            os.close(fd)


def check_lane(action: str, uri: str) -> None:
    """Check local libvirt, host architecture, guest acceleration and KVM when required."""
    if action not in {"native-x86", "native-power", "tcg-host"}:
        raise PreflightError(f"unknown lane {action!r}")
    _local_uri(uri)
    host = platform.machine()
    required_host = "ppc64le" if action == "native-power" else "x86_64"
    if host != required_host:
        raise PreflightError(f"{action} requires {required_host} host; observed {host}")
    caps_host, guests = _capabilities(uri)
    if caps_host != host:
        raise PreflightError(
            f"local host {host} disagrees with libvirt capabilities host {caps_host}; check URI"
        )
    guest = "ppc64le" if action in {"native-power", "tcg-host"} else "x86_64"
    expected_accel = "tcg" if action == "tcg-host" else "kvm"
    selected = guests.get(guest)
    if selected is None or selected["accel"] != expected_accel:
        raise PreflightError(
            f"{action} requires {guest} {expected_accel.upper()} capability; "
            "configure the guest emulator and acceleration"
        )
    if action == "tcg-host":
        emulator = Path(selected["emulator"])
        if not emulator.is_file() or not os.access(emulator, os.X_OK):
            raise PreflightError(
                f"reported ppc64le TCG emulator {emulator} is missing or nonexecutable; "
                "install qemu-system-ppc"
            )
    else:
        _check_kvm(host, power_hv=action == "native-power")


def _positive(raw: str, name: str) -> int:
    if not re.fullmatch(r"[0-9]+", raw) or len(raw) > 20 or int(raw) == 0:
        raise PreflightError(f"{name} must be a positive integer")
    return int(raw)


def _available_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        count = os.cpu_count()
        if count is None:
            raise PreflightError(
                "CPU count unavailable; run on a Linux host with visible CPUs"
            ) from None
        return count
    except OSError as exc:
        raise PreflightError(
            f"CPU affinity unavailable; inspect host CPU visibility ({exc})"
        ) from exc


def _available_memory_bytes() -> int:
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        for line in lines:
            match = re.fullmatch(r"MemAvailable:\s*([0-9]+)\s+kB", line)
            if match:
                return int(match.group(1)) * _KIB
    except OSError as exc:
        raise PreflightError(f"cannot read /proc/meminfo MemAvailable: {exc}") from exc
    raise PreflightError(
        "/proc/meminfo lacks MemAvailable; use a Linux host with observable memory"
    )


def check_capacity(
    workspace: Path,
    cpus: str = DEFAULT_CPUS,
    memory_mib: str = DEFAULT_MEMORY_MIB,
    disk_bytes: str = DEFAULT_DISK_BYTES,
) -> None:
    """Require currently free CPU, memory and workspace disk; make no reservation claim."""
    requested_cpus = _positive(cpus, "CPU count")
    requested_memory = _positive(memory_mib, "memory MiB") * _MIB
    requested_disk = _positive(disk_bytes, "disk bytes")
    if not workspace.is_dir():
        raise PreflightError(f"lane workspace {workspace} is not an existing directory")
    available_cpus = _available_cpus()
    if requested_cpus > available_cpus:
        raise PreflightError(
            f"CPU requirement {requested_cpus} exceeds {available_cpus} available; "
            "reduce lane size or choose another host"
        )
    available_memory = _available_memory_bytes()
    if requested_memory > available_memory:
        raise PreflightError(
            f"memory requirement {requested_memory} bytes exceeds {available_memory} available; "
            "free memory or choose another host"
        )
    try:
        free_disk = shutil.disk_usage(workspace).free
    except OSError as exc:
        raise PreflightError(f"cannot read disk free for {workspace}: {exc}") from exc
    if requested_disk > free_disk:
        raise PreflightError(
            f"disk requirement {requested_disk} bytes exceeds {free_disk} free at {workspace}; "
            "free space or choose another workspace"
        )


def main(args: list[str]) -> int:
    try:
        if len(args) == 2 and args[0] in {"native-x86", "native-power", "tcg-host"}:
            check_lane(args[0], args[1])
        elif len(args) == 5 and args[0] == "capacity":
            check_capacity(Path(args[1]), args[2], args[3], args[4])
        else:
            raise PreflightError(
                "usage: live_vm_lane.py <native-x86|native-power|tcg-host> URI | "
                "capacity WORKSPACE CPUS MEMORY_MIB DISK_BYTES"
            )
    except PreflightError as exc:
        print(f"live_vm lane preflight: {exc}", file=sys.stderr)
        return 1
    print(
        f"live_vm lane preflight: {args[0]} prerequisites available (read-only check)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
