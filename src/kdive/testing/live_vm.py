"""Reusable ``live_vm`` throwaway-domain harness (epic #1289, sub-issue A).

This module is the single reusable way to boot a throwaway libvirt domain, wait for a chosen
condition, and tear it down, with the environment quirks encoded once. It is **pytest-free** (the
mechanism ships in ``src/`` like ``kdive.mcp.dev_harness``), imports ``libvirt`` lazily so it loads
on a host without it, and reads **no** ``KDIVE_*`` env directly — the env-contract resolution and
the ``pytest.skip`` gates live in ``tests/live_vm`` (test-environment config, kept out of ``src/``
so it does not trip the ADR-0087 config-env guard, which reserves ``KDIVE_*`` reads for
``kdive.config``). A caller resolves its contract there and threads the rootfs + resolved URI into
``boot_throwaway_domain``.

Environment quirks this harness encodes so callers do not relearn them:

- libvirt mode is **per test**, not a global pin: traffic-capture uses ``qemu:///session``
  (unprivileged, dodges the ADR-0223 root-readback wall, #1258); snapshot uses ``qemu:///system``.
- Session mode: ``prepare_session_runtime`` redirects ``XDG_CONFIG_HOME`` to a short ``/tmp`` path
  for the QMP UNIX-socket 108-byte limit and restores it in teardown. This mutation is
  process-global, so **one session-mode boot at a time per process** (pytest-xdist workers are
  separate processes with independent ``os.environ``, so xdist is unaffected; nested/threaded
  same-process session boots are not supported).
- Staged overlays are created **beside the rootfs** so they inherit its libvirt access + SELinux
  ``virt_image_t`` label (a rootfs under ``$HOME``/``data_home_t`` is blocked at domain start under
  system mode — name it, do not silently fail).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import arch_traits
from kdive.providers.shared.libvirt_xml import QEMU_NS, register_qemu_namespace

_LOOPBACK_HOST = "127.0.0.1"
_PANIC_MARKER = "Kernel panic"
_SSH_ID_PREFIX = b"SSH-"
_POLL_INTERVAL_S = 0.5
_VALID_WAITS = ("active", "panic", "ssh")
# The default SSH-forward netdev id. A caller that pairs the domain with the local-libvirt traffic
# capturer must pass that provider's own SYSTEM_SSH_NETDEV_ID (the value the filter-dump targets)
# from the test side — this ``src/`` module does not import provider internals (the
# provider-boundary guard), so the default is a plain literal the coupled caller overrides.
_DEFAULT_SSH_NETDEV_ID = "kdivessh"


def throwaway_domain_xml(
    *,
    name: str,
    arch: str,
    disk_path: str,
    memory_mb: int = 1024,
    vcpu: int = 1,
    kernel_path: Path | None = None,
    cmdline: str | None = None,
    console_log: Path | None = None,
    ssh_hostfwd_port: int | None = None,
    ssh_netdev_id: str = _DEFAULT_SSH_NETDEV_ID,
) -> str:
    """Render a throwaway KVM domain, consuming every arch-varying fact of ``arch_traits(arch)``.

    Unlike production ``render_domain_xml`` this takes no ``ProvisioningProfile`` (a throwaway has
    no System). It emits the load-bearing ``<cpu mode>`` (ADR-0294: a missing ``<cpu>`` gives an
    EL9 guest ``qemu64``/x86-64-v1 and aborts PID 1) and the x86 ``<features>`` block so a KVM
    throwaway can boot a RHEL-family guest to userspace. ``<serial>``/``<console>`` are always
    emitted; ``console_log`` only adds the ``<log>`` sink. Built with ElementTree — no path injects
    XML. Raises ``CONFIGURATION_ERROR`` (via ``arch_traits``) for an unknown arch.
    """
    register_qemu_namespace()
    traits = arch_traits(arch)
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = name
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mb)
    ET.SubElement(domain, "vcpu").text = str(vcpu)
    ET.SubElement(domain, "cpu", mode=traits.kvm_cpu_mode)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=arch, machine=traits.machine).text = "hvm"
    if kernel_path is not None:
        ET.SubElement(os_el, "kernel").text = str(kernel_path)
        resolved_cmdline = (
            cmdline if cmdline is not None else f"root=/dev/vda console={traits.console_device} rw"
        )
        ET.SubElement(os_el, "cmdline").text = resolved_cmdline
    if traits.emit_acpi_features:
        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "vmcoreinfo", state="on")
    devices = ET.SubElement(domain, "devices")
    _append_root_disk(devices, disk_path)
    _append_serial(devices, console_log)
    if ssh_hostfwd_port is not None:
        _append_ssh_netdev(
            domain, ssh_hostfwd_port, netdev_id=ssh_netdev_id, pin_nic_slot=traits.pin_nic_slot
        )
    return ET.tostring(domain, encoding="unicode")


def _append_root_disk(devices: ET.Element, disk_path: str) -> None:
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")


def _append_serial(devices: ET.Element, console_log: Path | None) -> None:
    serial = ET.SubElement(devices, "serial", type="pty")
    if console_log is not None:
        ET.SubElement(serial, "log", file=str(console_log), append="off")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")


def _append_ssh_netdev(
    domain: ET.Element, port: int, *, netdev_id: str, pin_nic_slot: bool
) -> None:
    commandline = ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")
    netdev = f"user,id={netdev_id},hostfwd=tcp:{_LOOPBACK_HOST}:{port}-:22"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-netdev")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=netdev)
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-device")
    device = f"virtio-net-pci,netdev={netdev_id}"
    if pin_nic_slot:
        device = f"{device},addr=0x10"
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=device)


class _ActiveDomain(Protocol):
    """The narrow slice of the libvirt ``virDomain`` the active-wait needs (no libvirt stubs)."""

    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name


class _ThrowawayDomain(_ActiveDomain, Protocol):
    """The libvirt ``virDomain`` slice ``boot_throwaway_domain`` drives (no libvirt stubs).

    The action methods are typed ``-> object`` so both the real binding (which returns ``int``)
    and a test fake (which returns ``None``) structurally satisfy the protocol; the harness never
    reads the return values.
    """

    def create(self) -> object: ...
    def destroy(self) -> object: ...
    def undefineFlags(self, flags: int) -> object: ...  # noqa: N802 - libvirt binding name


class _ThrowawayConn(Protocol):
    """The libvirt ``virConnect`` slice ``boot_throwaway_domain`` drives (no libvirt stubs)."""

    def defineXML(self, xml: str) -> _ThrowawayDomain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> object: ...


def wait_for_active(
    domain: _ActiveDomain, deadline_s: float, *, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Poll ``domain.isActive()`` until true or the deadline passes."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if domain.isActive():
            return True
        sleep(_POLL_INTERVAL_S)
    return bool(domain.isActive())


def wait_for_panic(
    console_log: Path, deadline_s: float, *, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Poll the serial console file for the panic marker until it appears or the deadline passes."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if _PANIC_MARKER in console_log.read_text(errors="replace"):
            return True
        sleep(_POLL_INTERVAL_S)
    return _PANIC_MARKER in console_log.read_text(errors="replace")


def _ssh_banner_verdict(buffer: bytes) -> bool | None:
    if buffer.startswith(_SSH_ID_PREFIX):
        return True
    if not _SSH_ID_PREFIX.startswith(buffer):
        return False
    return None


def ssh_banner_reachable(  # pragma: no cover - live_vm
    host: str, port: int, timeout_s: float = 2.0
) -> bool:
    """One connect + sshd identification-banner read; True iff the peer speaks SSH.

    The harness owns its own probe (rather than importing the provider-internal, live-only
    ``_real_ssh_connect``) so test-support code does not reach into provider privates.
    """
    deadline = time.monotonic() + timeout_s
    sock = socket.create_connection((host, port), timeout=timeout_s)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue
            if not chunk:
                break
            buffer += chunk
            verdict = _ssh_banner_verdict(buffer)
            if verdict is not None:
                return verdict
    finally:
        sock.close()
    return False


def wait_for_ssh(
    host: str,
    port: int,
    deadline_s: float,
    *,
    probe: Callable[[str, int], bool] = ssh_banner_reachable,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll ``probe(host, port)`` until it returns True or the deadline passes.

    ``probe`` is one single-shot attempt (default the real banner probe); this is the missing outer
    loop, retrying past a refused/hanging port. Injected in tests to exercise the loop without a
    live guest. ``deadline_s`` bounds the whole wait; the probe's own timeout bounds each attempt.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if probe(host, port):
                return True
        except OSError:
            pass
        sleep(_POLL_INTERVAL_S)
    return False


def create_overlay(base: Path, dest: Path) -> None:
    """Create a qcow2 overlay at ``dest`` backed by ``base`` (staged beside it for the label)."""
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(dest)],
        check=True,
        capture_output=True,
    )


@dataclass(slots=True)
class _SessionRuntime:
    """Records the XDG_CONFIG_HOME redirect for a session-mode boot so teardown can restore it."""

    prior: str | None
    short_dir: Path

    def restore(self) -> None:
        if self.prior is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.prior
        with contextlib.suppress(OSError):
            self.short_dir.rmdir()


def prepare_session_runtime(uri: str) -> _SessionRuntime | None:
    """Redirect XDG_CONFIG_HOME to a short /tmp path for a session URI; None for system mode.

    Session-mode libvirt derives its per-domain QMP socket under $XDG_CONFIG_HOME; a deep pytest
    tmp path overflows the 108-byte UNIX socket limit. Process-global — one session-mode boot at a
    time per process (see module docstring).
    """
    if not uri.startswith("qemu:///session"):
        return None
    prior = os.environ.get("XDG_CONFIG_HOME")
    short_dir = Path(f"/tmp/kdive-cl-{uuid.uuid4().hex[:8]}")  # noqa: S108 - short path for QMP socket
    short_dir.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = str(short_dir)
    return _SessionRuntime(prior=prior, short_dir=short_dir)


def connect_libvirt(uri: str) -> _ThrowawayConn:  # pragma: no cover - live_vm
    """Open a libvirt connection. Call ``prepare_session_runtime`` first for a session URI.

    ``virConnect`` structurally satisfies the narrow ``_ThrowawayConn`` protocol.
    """
    import libvirt  # noqa: PLC0415  # operator-provided

    return libvirt.open(uri)


@dataclass(frozen=True, slots=True)
class LiveDomain:
    """A booted throwaway domain the harness yields: the live libvirt handles + boot inputs."""

    name: str
    domain: object
    conn: object
    uri: str
    ssh_port: int | None
    console_log: Path | None


class LiveVmBootTimeout(Exception):
    """A throwaway domain did not reach its wait condition before the deadline."""


def _validate_wait(
    wait_for: str, *, ssh_hostfwd_port: int | None, console_log: Path | None
) -> None:
    if wait_for not in _VALID_WAITS:
        raise CategorizedError(
            f"unknown wait_for {wait_for!r}; expected one of {_VALID_WAITS}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if wait_for == "ssh" and ssh_hostfwd_port is None:
        raise CategorizedError(
            'wait_for="ssh" requires ssh_hostfwd_port so the probe has a port to reach',
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if wait_for == "panic" and console_log is None:
        raise CategorizedError(
            'wait_for="panic" requires console_log so the panic-wait can read the serial console',
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _await_condition(
    wait_for: str,
    domain: _ActiveDomain,
    *,
    deadline_s: float,
    ssh_port: int | None,
    console_log: Path | None,
) -> bool:
    if wait_for == "active":
        return wait_for_active(domain, deadline_s)
    if wait_for == "panic":
        assert console_log is not None
        return wait_for_panic(console_log, deadline_s)
    assert ssh_port is not None
    return wait_for_ssh(_LOOPBACK_HOST, ssh_port, deadline_s)


@contextmanager
def boot_throwaway_domain(
    rootfs: Path,
    *,
    arch: str,
    name: str,
    mode: str = "qemu:///system",
    memory_mb: int = 1024,
    vcpu: int = 1,
    ssh_hostfwd_port: int | None = None,
    ssh_netdev_id: str = _DEFAULT_SSH_NETDEV_ID,
    kernel_path: Path | None = None,
    cmdline: str | None = None,
    console_log: Path | None = None,
    wait_for: str = "active",
    wait_timeout_s: float = 30.0,
    settle_s: float = 0.0,
    _connect: Callable[[str], _ThrowawayConn] = connect_libvirt,
    _overlay: Callable[[Path, Path], None] = create_overlay,
    _sleep: Callable[[float], None] = time.sleep,
) -> Iterator[LiveDomain]:
    """Boot a throwaway libvirt domain, wait for ``wait_for``, yield it, and guarantee teardown.

    See the module docstring for the environment contract. ``settle_s`` sleeps after the condition
    is reached (preserves the legacy ``create(); sleep(2)`` window). ``_connect``/``_overlay``/
    ``_sleep`` are injection seams for the unit tests; live callers use the defaults.

    ``import libvirt`` happens **only in the finally**, and only when a domain was defined — so the
    ``wait_for`` precondition guards raise before any libvirt import (they run on a libvirt-less
    host) and a boot unit test can stub ``sys.modules["libvirt"]`` to exercise the real teardown.
    """
    _validate_wait(wait_for, ssh_hostfwd_port=ssh_hostfwd_port, console_log=console_log)
    dest = rootfs.with_name(f"{name}.qcow2")
    runtime = prepare_session_runtime(mode)
    conn: _ThrowawayConn | None = None
    domain: _ThrowawayDomain | None = None
    try:
        _overlay(rootfs, dest)
        conn = _connect(mode)
        xml = throwaway_domain_xml(
            name=name,
            arch=arch,
            disk_path=str(dest),
            memory_mb=memory_mb,
            vcpu=vcpu,
            kernel_path=kernel_path,
            cmdline=cmdline,
            console_log=console_log,
            ssh_hostfwd_port=ssh_hostfwd_port,
            ssh_netdev_id=ssh_netdev_id,
        )
        domain = conn.defineXML(xml)
        domain.create()
        if not _await_condition(
            wait_for,
            domain,
            deadline_s=wait_timeout_s,
            ssh_port=ssh_hostfwd_port,
            console_log=console_log,
        ):
            raise LiveVmBootTimeout(
                f"domain {name!r} (mode {mode}) did not reach wait_for={wait_for!r} in "
                f"{wait_timeout_s}s"
            )
        if settle_s > 0:
            _sleep(settle_s)
        yield LiveDomain(
            name=name,
            domain=domain,
            conn=conn,
            uri=mode,
            ssh_port=ssh_hostfwd_port,
            console_log=console_log,
        )
    finally:
        if domain is not None:
            import libvirt  # noqa: PLC0415  # operator-provided; only reached once a domain exists

            with contextlib.suppress(libvirt.libvirtError):
                if domain.isActive():
                    domain.destroy()
            with contextlib.suppress(libvirt.libvirtError):
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        if runtime is not None:
            runtime.restore()
