"""LocalLibvirtInstall provider tests — injected fakes, no live host (ADR-0030)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET  # noqa: S405 - parses only self-rendered, trusted test XML
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import libvirt
import pytest

import kdive.config as config
from kdive.artifacts.storage import FetchedArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.boot import readiness as readiness_mod
from kdive.providers.local_libvirt.lifecycle.boot.guest_kernel_writer import (
    GuestKernelWriter,
    _kernel_dest,
    _RealGuestKernelWriter,
    _verify_kernel_size,
    _verify_vmlinux_size,
    _vmlinux_dest,
)
from kdive.providers.local_libvirt.lifecycle.boot.kernel_bundle import repack_modules_subtree
from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    ConsoleVerdict,
    ReadinessResult,
    _verdict_to_result,
    classify_console,
    first_crash_signature,
)
from kdive.providers.local_libvirt.lifecycle.install import (
    Fetch,
    LocalLibvirtInstall,
    _boot_window_polls,
    _stage_object,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER
from kdive.providers.ports.lifecycle import InstallRequest
from kdive.providers.shared.runtime_paths import read_console_log
from tests.live_vm import require_live_vm_provisioned
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn

_SYS = UUID("11111111-1111-1111-1111-111111111111")
_RUN = UUID("22222222-2222-2222-2222-222222222222")
_KERNEL_REF = "local/runs/22222222-2222-2222-2222-222222222222/kernel"
_INITRD_REF = "local/runs/22222222-2222-2222-2222-222222222222/initrd"
_CMDLINE = "console=ttyS0 crashkernel=256M"
_MODULES_VERSION = "6.9.0"

# Arch-varying boot members for the byte-agnostic install path (#1146). x86 is the default so
# every existing test stays byte-identical; ppc64le is an ELF64-LE header pinned to EM_PPC64 at
# offset 0x12 (the shape a validated ppc64le upload carries, ADR-0343) + an arch-suffixed version.
_X86_BOOT_MEMBER = b"bzImage-bytes"
_PPC64LE_BOOT_MEMBER = (
    b"\x7fELF\x02\x01" + b"\x00" * 12 + (21).to_bytes(2, "little") + b"ppc64le-vm"
)
_PPC64LE_MODULES_VERSION = "6.19.10-300.fc44.ppc64le"


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _combined_kernel_tar_bytes(
    *,
    with_modules: bool = True,
    version: str = _MODULES_VERSION,
    boot_bytes: bytes = _X86_BOOT_MEMBER,
) -> bytes:
    """The unified `kernel` artifact: gzip tar of boot/vmlinuz + (optionally) lib/modules/<ver>/.

    ``boot_bytes`` is the raw ``boot/vmlinuz`` payload — a bzImage blob by default, or an ELF for a
    ppc64le bundle (#1146). Extraction is byte-agnostic, so the payload is opaque to the tar shape.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, "boot/vmlinuz", boot_bytes)
        if with_modules:
            _tar_add(tar, f"lib/modules/{version}/modules.dep", b"")
            _tar_add(tar, f"lib/modules/{version}/kernel/drivers/virtio_blk.ko", b"module-bytes")
    return buf.getvalue()


def _modules_only_tar_bytes(version: str) -> bytes:
    """A lib/modules/<ver>/ tar in the build layout (the injector's _read_release input)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, f"lib/modules/{version}/kernel/foo.ko", b"\x7fELF stub")
        _tar_add(tar, f"lib/modules/{version}/modules.order", b"kernel/foo.ko\n")
    return buf.getvalue()


@dataclass
class _Fetch:
    """Records (ref, dest); writes a combined kernel tar via temp-then-rename.

    The kernel fetch must produce the unified combined tar so install's host-side
    ``extract_boot_vmlinuz``/``repack_modules_subtree`` succeed. ``with_modules=False`` writes a
    tar with only ``boot/vmlinuz`` (no ``lib/modules/``) to exercise the modules-absent path.
    """

    calls: list[tuple[str, Path]] = field(default_factory=list)
    fail: bool = False
    with_modules: bool = True
    boot_bytes: bytes = _X86_BOOT_MEMBER
    version: str = _MODULES_VERSION

    def __call__(self, ref: str, dest: Path) -> None:
        self.calls.append((ref, dest))
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(
            _combined_kernel_tar_bytes(
                with_modules=self.with_modules, version=self.version, boot_bytes=self.boot_bytes
            )
        )
        if self.fail:
            raise CategorizedError("synthetic fetch failure", category=ErrorCategory.STALE_HANDLE)
        tmp.rename(dest)


@dataclass
class _Readiness:
    """Canned readiness seam. answered=False → never-answered; ok=False → answered-fail."""

    answered: bool = True
    ok: bool = True
    probe_error: str | None = None
    calls: int = 0

    def readiness(self, system_id: UUID) -> ReadinessResult:
        self.calls += 1
        return ReadinessResult(answered=self.answered, ok=self.ok, probe_error=self.probe_error)


@dataclass
class _FakeKernelWriter:
    """Records an "inject" into the shared events list; ``fail`` raises INFRASTRUCTURE_FAILURE.

    Captures the kernel image and modules tarball paths it was handed so a test can assert the
    install plane stages the from-source kernel alongside the modules (ADR-0207).
    """

    events: list[str]
    injected: bool = False
    fail: bool = False
    kernel_image: Path | None = None
    modules_tar: Path | None = None
    modules_tar_existed: bool = False
    modules_version: str | None = None
    vmlinux: Path | None = None

    def inject(
        self, overlay: str, kernel_image: Path, modules_tar: Path, vmlinux: Path | None = None
    ) -> None:
        self.events.append("inject")
        self.kernel_image = kernel_image
        self.modules_tar = modules_tar
        self.modules_tar_existed = modules_tar.exists()  # captured before install reclaims it
        if self.modules_tar_existed:
            # Reuse the production parser the real writer keys depmod off, so the fake cannot drift.
            self.modules_version = _RealGuestKernelWriter._read_release(modules_tar, "overlay")
        self.vmlinux = vmlinux
        if self.fail:
            raise CategorizedError(
                "synthetic inject failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        self.injected = True


@dataclass
class _RecordingFetch:
    """Records a "fetch" into the shared events list and captures the fetched refs."""

    events: list[str]
    refs: list[str] = field(default_factory=list)

    def __call__(self, ref: str, dest: Path) -> None:
        self.events.append("fetch")
        self.refs.append(ref)


@dataclass
class _EventDomain(FakeDomain):
    """A FakeDomain that records ``destroy`` into a shared events list (force-off ordering)."""

    events: list[str] = field(default_factory=list)

    def destroy(self) -> int:
        self.events.append("destroy")
        return super().destroy()


def _existing_domain(events: list[str] | None = None) -> FakeDomain:
    """The domain provisioning already defined (no <os> direct-kernel section yet).

    When ``events`` is supplied the domain reports ``isActive() == 1`` and records its
    ``destroy`` into the shared list so the install force-off ordering is observable.
    """
    if events is None:
        return FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS))
    return _EventDomain(
        domain_name=f"kdive-{_SYS}", system_id=str(_SYS), active=True, events=events
    )


def _conn_with_existing(
    *, define_error: int | None = None, events: list[str] | None = None
) -> FakeLibvirtConn:
    domain = _existing_domain(events)
    return FakeLibvirtConn(lookup={domain.domain_name: domain}, define_error=define_error)


def _install(
    *,
    conn: FakeLibvirtConn,
    fetch: _Fetch | None = None,
    seam: _Readiness | None = None,
    staging_root: Path,
    kernel_writer: GuestKernelWriter | None = None,
    fetch_modules: Fetch | None = None,
) -> LocalLibvirtInstall:
    fetch = fetch or _Fetch()
    seam = seam or _Readiness()
    return LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=fetch,
        fetch_initrd=fetch,
        readiness=seam.readiness,
        staging_root=staging_root,
        boot_window_polls=3,
        fetch_modules=fetch_modules or fetch,
        kernel_writer=kernel_writer,
    )


def test_boot_window_polls_default_is_180(monkeypatch: pytest.MonkeyPatch) -> None:
    # 900 s default / 5 s poll cadence = 180 polls (the widened POWER9-friendly window).
    monkeypatch.delenv("KDIVE_LIBVIRT_BOOT_WINDOW_S", raising=False)
    assert _boot_window_polls() == 180


def test_boot_window_polls_rounds_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # A window not divisible by the 5 s cadence rounds up (math.ceil), so the last partial
    # interval is still polled — 902 / 5 = 180.4 -> 181, never truncated to 180.
    monkeypatch.setenv("KDIVE_LIBVIRT_BOOT_WINDOW_S", "902")
    assert _boot_window_polls() == 181


def test_boot_window_polls_honors_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_BOOT_WINDOW_S", "1000")
    assert _boot_window_polls() == 200


def _request(
    *,
    cmdline: str = _CMDLINE,
    method: CaptureMethod = CaptureMethod.HOST_DUMP,
    initrd_ref: str | None = None,
    debuginfo_ref: str | None = None,
) -> InstallRequest:
    return InstallRequest(
        system_id=_SYS,
        run_id=_RUN,
        kernel_ref=_KERNEL_REF,
        cmdline=cmdline,
        method=method,
        initrd_ref=initrd_ref,
        debuginfo_ref=debuginfo_ref,
    )


# --- install: render + staging -------------------------------------------------------


def test_install_redefines_direct_kernel_os(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    assert len(conn.defined_xml) == 1
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    kernel = os_el.find("kernel")
    initrd = os_el.find("initrd")
    cmdline = os_el.find("cmdline")
    assert kernel is not None and initrd is not None and cmdline is not None
    assert cmdline.text == _CMDLINE
    # The kernel/initrd point at the per-Run staging path …/{system_id}/{run_id}/….
    assert kernel.text is not None and f"{_SYS}/{_RUN}" in kernel.text
    assert initrd.text is not None and f"{_SYS}/{_RUN}" in initrd.text


def test_install_stages_kernel_and_initrd_to_per_run_path(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    fetch = _Fetch()
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert (staged_dir / "kernel").exists()
    assert (staged_dir / "initrd").exists()
    # No leftover temp file from the temp-then-rename.
    assert list(staged_dir.glob("*.part")) == []


def test_install_reclaims_the_redundant_combined_tar(tmp_path: Path) -> None:
    # boot/vmlinuz is extracted to staging/kernel (the <kernel> element), so the fetched combined
    # tar is dead weight afterward — install must not retain a redundant copy of the kernel bytes
    # for the System's lifetime.
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert (staged_dir / "kernel").exists()  # the <kernel> image persists
    assert not (staged_dir / "kernel.tar.gz").exists()  # the combined tar is reclaimed


def test_install_kdump_reclaims_the_repacked_modules_tar(tmp_path: Path) -> None:
    # The repacked modules tar is injected in-guest during install and is unused afterward.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer)

    inst.install(_request(method=CaptureMethod.KDUMP))

    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert writer.injected
    assert not (staged_dir / "modules.tar.gz").exists()
    assert not (staged_dir / "kernel.tar.gz").exists()


def test_repack_modules_subtree_skips_path_traversal_members(tmp_path: Path) -> None:
    # An externally-uploaded kernel tar whose lib/modules member escapes via ``..`` must not be
    # carried into the in-guest extract; the traversal member is dropped, the legit one kept.
    combined = tmp_path / "kernel.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, "boot/vmlinuz", b"bz")
        _tar_add(tar, "lib/modules/6.9.0/../../../etc/evil", b"pwn")
        _tar_add(tar, "lib/modules/6.9.0/kernel/ok.ko", b"mod")
    combined.write_bytes(buf.getvalue())

    out = tmp_path / "modules.tar.gz"
    assert repack_modules_subtree(combined, out)
    with tarfile.open(out, "r:gz") as repacked:
        names = set(repacked.getnames())
    assert "lib/modules/6.9.0/kernel/ok.ko" in names
    assert not any(".." in n.split("/") for n in names)


@pytest.mark.parametrize("prefix", ("./", "/"))
def test_repack_modules_subtree_normalizes_prefixed_members(tmp_path: Path, prefix: str) -> None:
    version = "7.0.0-dirty"
    combined = tmp_path / "kernel.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, f"{prefix}boot/vmlinuz", b"bz")
        _tar_add(tar, f"{prefix}lib/modules/{version}/modules.dep", b"")
        _tar_add(tar, f"{prefix}lib/modules/{version}/kernel/ok.ko", b"mod")
    combined.write_bytes(buf.getvalue())

    out = tmp_path / "modules.tar.gz"
    assert repack_modules_subtree(combined, out)

    with tarfile.open(out, "r:gz") as repacked:
        names = set(repacked.getnames())
    assert names == {
        f"lib/modules/{version}/modules.dep",
        f"lib/modules/{version}/kernel/ok.ko",
    }
    assert _RealGuestKernelWriter._read_release(out, "ov") == version


def test_install_does_not_inject_xml_from_cmdline(tmp_path: Path) -> None:
    # A hostile cmdline value must be carried as text, not parsed as markup.
    hostile = "crashkernel=256M </cmdline><evil/>"
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(cmdline=hostile))
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None and os_el.find("evil") is None  # not injected
    cmdline = os_el.find("cmdline")
    assert cmdline is not None and cmdline.text == hostile  # carried verbatim


def test_install_renders_tuned_crashkernel_into_domain_cmdline(tmp_path: Path) -> None:
    # Acceptance (#989, ADR-0300): a per-install crashkernel=512M composed upstream by cmdline_for
    # reaches the domain <cmdline> verbatim — the installer renders whatever cmdline it is handed.
    tuned = "console=ttyS0 root=/dev/vda crashkernel=512M"
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(cmdline=tuned))
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    cmdline = os_el.find("cmdline")
    assert cmdline is not None and cmdline.text == tuned
    assert "crashkernel=512M" in (cmdline.text or "")


def test_install_preserves_the_gdbstub_qemu_commandline(tmp_path: Path) -> None:
    # A gdbstub-provisioned domain's <qemu:commandline> must survive the install os-edit
    # round-trip with its qemu: prefix — otherwise ElementTree re-prefixes it (ns0:) and libvirt
    # drops the gdbstub, breaking the live debug attach. Guards the namespace registration in
    # install._render_os_section.
    from kdive.providers.shared.libvirt_xml import QEMU_NS, recorded_gdb_port

    provisioned = (
        f'<domain type="kvm" xmlns:qemu="{QEMU_NS}">'
        f"<name>kdive-{_SYS}</name>"
        '<metadata><kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">'
        f"{_SYS}</kdive:system></metadata>"
        "<qemu:commandline><qemu:arg value='-gdb'/>"
        "<qemu:arg value='tcp:127.0.0.1:4444'/></qemu:commandline>"
        "</domain>"
    )
    domain = FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS), xml_desc=provisioned)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    redefined = conn.defined_xml[0]
    assert "qemu:commandline" in redefined  # prefix preserved, not ns0:
    assert recorded_gdb_port(redefined) == 4444


def test_install_preserves_the_ssh_forward_qemu_commandline(tmp_path: Path) -> None:
    # A drgn-live-provisioned domain's <qemu:commandline> SSH forward must survive the install
    # os-edit round-trip with its qemu: prefix — same namespace hazard as the gdbstub arg
    # (ADR-0218 §3). Guards the register_qemu_namespace call in install._render_os_section for the
    # SSH element this PR introduces.
    from kdive.providers.shared.libvirt_xml import QEMU_NS, recorded_ssh_port

    provisioned = (
        f'<domain type="kvm" xmlns:qemu="{QEMU_NS}">'
        f"<name>kdive-{_SYS}</name>"
        '<metadata><kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">'
        f"{_SYS}</kdive:system></metadata>"
        "<qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        "<qemu:arg value='user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:40022-:22'/>"
        "<qemu:arg value='-device'/>"
        "<qemu:arg value='virtio-net-pci,netdev=kdivessh,addr=0x10'/>"
        "</qemu:commandline>"
        "</domain>"
    )
    domain = FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS), xml_desc=provisioned)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.install(_request(initrd_ref=_INITRD_REF))

    redefined = conn.defined_xml[0]
    assert "qemu:commandline" in redefined  # prefix preserved, not ns0:
    assert recorded_ssh_port(redefined) == 40022


# --- install: kdump prerequisite -----------------------------------------------------


def test_install_kdump_without_modules_or_initrd_is_config_error_before_redefine(
    tmp_path: Path,
) -> None:
    # method=KDUMP whose combined kernel tar carries no lib/modules and with no initrd_ref: the
    # capture environment is absent → CONFIGURATION_ERROR, nothing redefined.
    conn = _conn_with_existing()
    inst = _install(conn=conn, fetch=_Fetch(with_modules=False), staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.KDUMP))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []  # nothing redefined on a missing capture path


def test_install_kdump_with_initrd_but_no_modules_proceeds(tmp_path: Path) -> None:
    # method=KDUMP whose combined tar has no lib/modules but a staged initrd present: the initrd
    # supplies the capture environment, so install proceeds and redefines once without injecting.
    conn = _conn_with_existing()
    inst = _install(conn=conn, fetch=_Fetch(with_modules=False), staging_root=tmp_path)
    inst.install(_request(method=CaptureMethod.KDUMP, initrd_ref=_INITRD_REF))
    assert len(conn.defined_xml) == 1  # redefined once, no CONFIGURATION_ERROR raised


def test_install_fadump_shares_the_kdump_capture_prerequisite(tmp_path: Path) -> None:
    # fadump reuses the kdump capture environment (ADR-0349): a FADUMP install with no modules and
    # no initrd hits the same absent-capture CONFIGURATION_ERROR as KDUMP, before any redefine.
    conn = _conn_with_existing()
    inst = _install(conn=conn, fetch=_Fetch(with_modules=False), staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.FADUMP))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.defined_xml == []


def test_install_fadump_injects_modules_like_kdump(tmp_path: Path) -> None:
    # fadump's second kernel needs the module tree just as kdump does, so needs_modules fires and
    # the injector runs for a FADUMP install (ADR-0349).
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.FADUMP))

    assert writer.injected


# --- install: module injection (from-source kdump lane) ------------------------------


def test_install_kdump_injects_modules_from_combined_tar_and_no_initrd_rendered(
    tmp_path: Path,
) -> None:
    # A kdump boot extracts boot/vmlinuz for the <kernel> element and injects the combined tar's
    # lib/modules subtree (repacked host-side) — no separate modules artifact, no separate fetch.
    events: list[str] = []
    conn = _conn_with_existing(events=events)  # its domain is active
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.KDUMP))

    assert writer.injected
    assert fetch.refs == []  # no separate modules fetch; modules come from the combined kernel tar
    # The writer is handed the per-Run staged kernel image (boot/vmlinuz extracted from the tar)
    # so it can also stage /boot/vmlinuz-<ver> in-guest (ADR-0207), and the repacked modules tar.
    assert writer.kernel_image == tmp_path / str(_SYS) / str(_RUN) / "kernel"
    assert writer.kernel_image is not None and writer.kernel_image.exists()
    assert writer.modules_tar == tmp_path / str(_SYS) / str(_RUN) / "modules.tar.gz"
    assert writer.modules_tar_existed  # the repacked tar was present when handed to the injector
    # force-off precedes the inject (no debuginfo fetch here).
    assert events.index("destroy") < events.index("inject")
    assert "fetch" not in events
    assert len(conn.defined_xml) == 1
    assert "<initrd>" not in conn.defined_xml[0]  # production boot has no separate initrd


def test_install_non_kdump_without_debuginfo_does_not_force_off_or_inject(tmp_path: Path) -> None:
    # Module injection (force-off + rw libguestfs mount) is gated on the KDUMP capture method OR a
    # debuginfo_ref (ADR-0221). A console/gdbstub build with no debuginfo_ref does not inject even
    # though its combined kernel tar carries lib/modules.
    events: list[str] = []
    conn = _conn_with_existing(events=events)  # its domain is active
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.CONSOLE))

    assert events == []  # no destroy, no fetch, no inject
    assert not writer.injected
    assert fetch.refs == []
    assert len(conn.defined_xml) == 1  # normal direct-kernel boot still defined


def test_install_kdump_with_debuginfo_fetches_and_stages_vmlinux(tmp_path: Path) -> None:
    # A kdump build with a debuginfo_ref (the DWARF vmlinux) stages it in-guest for live drgn,
    # riding the same rw injection session as the combined tar's modules (ADR-0221).
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.KDUMP, debuginfo_ref="runs/r/vmlinux"))

    assert writer.injected
    assert fetch.refs == ["runs/r/vmlinux"]  # only the DWARF vmlinux is fetched, not modules
    assert writer.vmlinux == tmp_path / str(_SYS) / str(_RUN) / "vmlinux"
    # force-off precedes the debuginfo fetch which precedes the inject.
    assert events.index("destroy") < events.index("fetch") < events.index("inject")


@pytest.mark.parametrize(
    ("boot_member", "version", "cmdline"),
    [
        (_X86_BOOT_MEMBER, _MODULES_VERSION, "console=ttyS0 root=/dev/vda crashkernel=256M"),
        (
            _PPC64LE_BOOT_MEMBER,
            _PPC64LE_MODULES_VERSION,
            "console=hvc0 root=/dev/vda crashkernel=512M",
        ),
    ],
    ids=["x86_64", "ppc64le"],
)
def test_install_stages_the_bundle_through_inject_and_render(
    boot_member: bytes, version: str, cmdline: str, tmp_path: Path
) -> None:
    # #1146: the install path is byte-agnostic — a ppc64le ELF boot/vmlinuz and a .ppc64le module
    # version flow through extract → repack → inject → <os> render exactly like an x86 bzImage.
    # This asserts the ORCHESTRATION (which bytes land where, the rendered <os>) with the fake
    # writer; the real writer's cross-arch module indexing is host-side depmod as of #1148
    # (unit-tested in test_module_indexing.py) and live-exercised in the #1148 kdump proof.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    fetch = _Fetch(boot_bytes=boot_member, version=version)
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path, kernel_writer=writer)

    inst.install(_request(method=CaptureMethod.KDUMP, cmdline=cmdline, initrd_ref=_INITRD_REF))

    # The staged <kernel> file is the boot member verbatim — extraction reads no magic.
    assert writer.injected
    assert writer.kernel_image is not None
    assert writer.kernel_image.read_bytes() == boot_member
    # The repacked modules tar carries the version verbatim (the .ppc64le suffix is preserved).
    assert writer.modules_version == version
    # Render: <kernel>/<initrd> at the per-Run staged path, <cmdline> passed through unchanged.
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    kernel, initrd, cmdline_el = os_el.find("kernel"), os_el.find("initrd"), os_el.find("cmdline")
    assert kernel is not None and kernel.text is not None and f"{_SYS}/{_RUN}" in kernel.text
    assert initrd is not None and initrd.text is not None and f"{_SYS}/{_RUN}" in initrd.text
    assert cmdline_el is not None and cmdline_el.text == cmdline


def test_install_host_dump_with_debuginfo_injects_vmlinux(tmp_path: Path) -> None:
    # The vmlinux trigger is independent of the capture method: a non-kdump System with a
    # debuginfo_ref still injects (modules from the combined tar + the DWARF vmlinux), so drgn-live
    # works without kdump.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.HOST_DUMP, debuginfo_ref="runs/r/vmlinux"))

    assert writer.injected
    assert fetch.refs == ["runs/r/vmlinux"]
    assert writer.vmlinux == tmp_path / str(_SYS) / str(_RUN) / "vmlinux"


def test_install_kdump_without_debuginfo_passes_no_vmlinux(tmp_path: Path) -> None:
    # The kdump path with no debuginfo_ref injects modules + kernel, handing the writer no vmlinux.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    inst = _install(
        conn=conn,
        staging_root=tmp_path,
        kernel_writer=writer,
        fetch_modules=_RecordingFetch(events),
    )

    inst.install(_request(method=CaptureMethod.KDUMP))

    assert writer.injected
    assert writer.vmlinux is None


def test_install_host_dump_without_debuginfo_does_not_inject(tmp_path: Path) -> None:
    # A non-kdump System with no debuginfo_ref triggers neither force-off nor inject.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events)
    fetch = _RecordingFetch(events)
    inst = _install(conn=conn, staging_root=tmp_path, kernel_writer=writer, fetch_modules=fetch)

    inst.install(_request(method=CaptureMethod.HOST_DUMP))

    assert events == []
    assert not writer.injected


def test_vmlinux_dest_is_drgn_discoverable_path() -> None:
    # drgn -k's debuginfo finder searches /usr/lib/debug/lib/modules/<uname -r>/vmlinux.
    assert _vmlinux_dest("7.0.0") == "/usr/lib/debug/lib/modules/7.0.0/vmlinux"


def test_verify_vmlinux_size_rejects_empty_upload() -> None:
    # A zero-byte vmlinux fails loud at injection (ADR-0221) rather than as an opaque in-guest
    # drgn ELF-parse error later.
    dest = "/usr/lib/debug/lib/modules/7.0.0/vmlinux"
    with pytest.raises(CategorizedError) as caught:
        _verify_vmlinux_size(0, "ov", dest)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"overlay": "ov", "dest": dest}


def test_verify_vmlinux_size_accepts_non_empty_upload() -> None:
    _verify_vmlinux_size(1, "ov", "/usr/lib/debug/lib/modules/7.0.0/vmlinux")


def test_install_kdump_force_off_precedes_mount_even_if_inject_fails(
    tmp_path: Path,
) -> None:
    # The corruption guard must fire before the writer touches the overlay, regardless of outcome.
    events: list[str] = []
    conn = _conn_with_existing(events=events)
    writer = _FakeKernelWriter(events, fail=True)
    inst = _install(
        conn=conn,
        staging_root=tmp_path,
        kernel_writer=writer,
        fetch_modules=_RecordingFetch(events),
    )

    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.KDUMP))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert events[0] == "destroy"  # force-off happened before the failed inject
    assert conn.defined_xml == []  # nothing redefined


def test_install_kdump_no_writer_is_missing_dependency(tmp_path: Path) -> None:
    # A kdump boot whose combined tar carries modules must inject them; without a configured
    # GuestKernelWriter that surfaces as MISSING_DEPENDENCY (not a silent skip).
    conn = _conn_with_existing()
    inst = _install(conn=conn, staging_root=tmp_path)  # no kernel_writer
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request(method=CaptureMethod.KDUMP))
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert conn.defined_xml == []


def test_kernel_writer_mount_close_failure_preserves_open_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Guest:
        launch_error = RuntimeError("launch failed")

        def add_drive_opts(self, _filename: str, *, format: str, readonly: bool) -> None:
            assert format == "qcow2"
            assert readonly is False

        def launch(self) -> None:
            raise self.launch_error

        def close(self) -> None:
            raise RuntimeError("close failed")

    guest = _Guest()
    monkeypatch.setitem(
        sys.modules,
        "guestfs",
        SimpleNamespace(GuestFS=lambda **_kwargs: guest),
    )

    with pytest.raises(CategorizedError) as caught:
        _RealGuestKernelWriter._mount_rw("overlay.qcow2")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"overlay": "overlay.qcow2", "error": "RuntimeError"}
    assert caught.value.__cause__ is guest.launch_error


def test_read_release_recovers_version_from_repacked_modules_tar(tmp_path: Path) -> None:
    # repack_modules_subtree writes members as ``lib/modules/<ver>/...``; _read_release must
    # recover ``<ver>`` from that exact layout (the build↔install bundle contract, #654). A
    # regression here is the double-nesting / depmod-"lib" bug the live path would otherwise hit.
    version = "7.0.0-kdive"
    combined = tmp_path / "kernel.tar.gz"
    combined.write_bytes(_combined_kernel_tar_bytes(version=version))
    modules_tar = tmp_path / "modules.tar.gz"
    assert repack_modules_subtree(combined, modules_tar)

    assert _RealGuestKernelWriter._read_release(modules_tar, "ov") == version


# --- install: kernel staging into the guest /boot (ADR-0207) -------------------------


def test_kernel_dest_is_boot_vmlinuz_for_version() -> None:
    # kdumpctl kexec-loads /boot/vmlinuz-$(uname -r); the staged kernel must land at exactly
    # that path. A typo (vmlinux-, missing -<ver>) would leave kdump unable to find the kernel.
    assert _kernel_dest("7.0.0") == "/boot/vmlinuz-7.0.0"


def test_kernel_dest_composes_with_read_release(tmp_path: Path) -> None:
    # The kernel filename's <ver> is the same modules-tarball release depmod indexed — one
    # version source for /lib/modules/<ver> and /boot/vmlinuz-<ver>. Feed a build-layout tarball
    # through _read_release, then _kernel_dest, and assert the full destination path.
    version = "7.0.0-kdive"
    tar_path = tmp_path / "modules.tar.gz"
    tar_path.write_bytes(_modules_only_tar_bytes(version))

    recovered = _RealGuestKernelWriter._read_release(tar_path, "ov")
    assert _kernel_dest(recovered) == f"/boot/vmlinuz-{version}"


def test_verify_kernel_size_rejects_empty_upload() -> None:
    # A zero-byte kernel in /boot is always a failed upload (unlike modules.dep, which is
    # validly empty for an all-builtin kernel) → typed INFRASTRUCTURE_FAILURE naming the overlay.
    with pytest.raises(CategorizedError) as caught:
        _verify_kernel_size(0, "ov", "/boot/vmlinuz-7.0.0")
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details["overlay"] == "ov"
    assert caught.value.details["dest"] == "/boot/vmlinuz-7.0.0"


def test_verify_kernel_size_accepts_non_empty_upload() -> None:
    # A non-empty kernel passes the sentinel (returns without raising).
    _verify_kernel_size(1, "ov", "/boot/vmlinuz-7.0.0")


# --- install: failures ---------------------------------------------------------------


def test_install_definexml_error_is_install_failure(tmp_path: Path) -> None:
    conn = _conn_with_existing(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.install(_request())
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


def test_install_fetch_failure_leaves_no_final_file(tmp_path: Path) -> None:
    conn = _conn_with_existing()
    fetch = _Fetch(fail=True)
    inst = _install(conn=conn, fetch=fetch, staging_root=tmp_path)
    with pytest.raises(CategorizedError):
        inst.install(_request())
    staged_dir = tmp_path / str(_SYS) / str(_RUN)
    assert not (staged_dir / "kernel").exists()  # rename never happened


# --- boot: power-cycle + readiness ---------------------------------------------------


def _domain(*, active: bool = False) -> FakeDomain:
    return FakeDomain(domain_name=f"kdive-{_SYS}", system_id=str(_SYS), active=active)


def test_boot_powercycles_running_domain_then_readiness(tmp_path: Path) -> None:
    domain = _domain(active=True)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.boot(_SYS)  # no raise
    assert domain.calls == ["destroy", "create"]  # running → destroy then create


def test_boot_starts_stopped_domain(tmp_path: Path) -> None:
    domain = _domain(active=False)
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    inst.boot(_SYS)
    assert domain.calls == ["create"]  # not running → just create


def test_boot_never_answered_is_boot_timeout(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT
    # The failure carries the System id under the documented detail key so an operator can
    # tie the timeout to a specific System.
    assert caught.value.details["system_id"] == str(_SYS)


def test_boot_timeout_includes_first_readiness_probe_error(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False, probe_error="virsh domstate timed out after 2s")
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)

    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)

    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT
    assert caught.value.details["probe_error"] == "virsh domstate timed out after 2s"


def test_boot_answered_but_failed_is_readiness_failure(tmp_path: Path) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=True, ok=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.READINESS_FAILURE


def test_boot_create_error_is_install_failure(tmp_path: Path) -> None:
    domain = FakeDomain(
        domain_name=f"kdive-{_SYS}",
        system_id=str(_SYS),
        raise_on={"create": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE


def test_boot_powercycle_error_is_install_failure_naming_the_verb(tmp_path: Path) -> None:
    # A running domain whose destroy fails surfaces a power-cycling install failure naming the
    # verb and the offending domain, so the message distinguishes it from a lookup/create fault.
    domain = FakeDomain(
        domain_name=f"kdive-{_SYS}",
        system_id=str(_SYS),
        active=True,
        raise_on={"destroy": libvirt.VIR_ERR_INTERNAL_ERROR},
    )
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE
    assert str(caught.value) == "libvirt error power-cycling domain"
    assert caught.value.details["domain"] == f"kdive-{_SYS}"


def test_boot_absent_domain_is_install_failure(tmp_path: Path) -> None:
    conn = FakeLibvirtConn(lookup={})
    inst = _install(conn=conn, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.INSTALL_FAILURE
    # The lookup failure names the verb and carries the domain under the documented key.
    assert str(caught.value) == "libvirt error looking up domain"
    assert caught.value.details["domain"] == f"kdive-{_SYS}"


# --- from_env does not connect/spawn -------------------------------------------------


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")

    def _no_open(*_: object, **__: object) -> object:
        raise AssertionError("from_env must not open a libvirt connection")

    monkeypatch.setattr(libvirt, "open", _no_open)
    inst = LocalLibvirtInstall.from_env()  # building must not connect
    assert isinstance(inst, LocalLibvirtInstall)


# --- read_console_log ----------------------------------------------------------------


def test_read_console_log_returns_bytes(tmp_path: Path) -> None:
    log = tmp_path / "sys.log"
    log.write_bytes(b"[ 0.0] Kernel panic - __d_lookup\n")
    assert b"__d_lookup" in read_console_log(log)


def test_read_console_log_missing_is_empty(tmp_path: Path) -> None:
    assert read_console_log(tmp_path / "absent.log") == b""


# --- method-conditional kdump + optional initrd --------------------------------------


def test_install_console_method_omits_initrd(tmp_path: Path) -> None:
    """CONSOLE method, no initrd_ref: no initrd fetched; no <initrd> in XML."""

    def _initrd_must_not_run(_ref: str, _dest: Path) -> None:
        raise AssertionError("initrd fetched when no initrd_ref given")

    def _fetch_combined(_ref: str, dest: Path) -> None:
        dest.write_bytes(_combined_kernel_tar_bytes())

    conn = _conn_with_existing()
    installer = LocalLibvirtInstall(
        connect=lambda: conn,
        fetch_kernel=_fetch_combined,
        fetch_initrd=_initrd_must_not_run,
        readiness=lambda _sid: ReadinessResult(answered=True, ok=True),
        staging_root=tmp_path,
        boot_window_polls=3,
    )
    # CONSOLE + no initrd_ref: no initrd fetched, no <initrd> rendered.
    installer.install(_request(cmdline="console=ttyS0", method=CaptureMethod.CONSOLE))
    assert len(conn.defined_xml) == 1
    domain = ET.fromstring(conn.defined_xml[0])  # noqa: S314 - self-rendered, trusted
    os_el = domain.find("os")
    assert os_el is not None
    assert os_el.find("initrd") is None


# --- _stage_object: object-store read → temp-then-rename ------------------------------


@dataclass
class _FakeStore:
    """Records the (ref, etag) of each get_artifact and returns canned bytes or raises."""

    data: bytes = b"bzimage-bytes"
    error: CategorizedError | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.calls.append((key, etag))
        if self.error is not None:
            raise self.error
        return FetchedArtifact(self.data, Sensitivity.SENSITIVE, "build")


def test_stage_object_writes_bytes_via_temp_then_rename(tmp_path: Path) -> None:
    store = _FakeStore(data=b"real-kernel")
    dest = tmp_path / "kernel"

    _stage_object(store, _KERNEL_REF, dest)

    assert dest.read_bytes() == b"real-kernel"
    # The temp file is renamed into place, never left behind.
    assert list(tmp_path.iterdir()) == [dest]


def test_stage_object_reads_unconditionally_with_none_etag(tmp_path: Path) -> None:
    store = _FakeStore()

    _stage_object(store, _KERNEL_REF, tmp_path / "kernel")

    # ADR-0054 regression guard: the seam must read with etag=None (an empty/non-None etag
    # would 412 on a real store). This is the only place the etag argument is chosen.
    assert store.calls == [(_KERNEL_REF, None)]


def test_stage_object_propagates_store_error_and_leaves_dest_intact(tmp_path: Path) -> None:
    dest = tmp_path / "kernel"
    dest.write_bytes(b"previously-staged")
    store = _FakeStore(
        error=CategorizedError("gone", category=ErrorCategory.STALE_HANDLE),
    )

    with pytest.raises(CategorizedError) as excinfo:
        _stage_object(store, _KERNEL_REF, dest)

    assert excinfo.value.category is ErrorCategory.STALE_HANDLE
    # A failed fetch leaves the prior file untouched and no partial temp behind.
    assert dest.read_bytes() == b"previously-staged"
    assert list(tmp_path.iterdir()) == [dest]


def test_stage_object_categorizes_local_write_failure(tmp_path: Path) -> None:
    dest = tmp_path / "kernel"
    # A directory at the .part path makes write_bytes raise IsADirectoryError (an OSError),
    # standing in for a disk-full/permission staging-write fault.
    (tmp_path / "kernel.part").mkdir()
    store = _FakeStore(data=b"kernel-bytes")

    with pytest.raises(CategorizedError) as excinfo:
        _stage_object(store, _KERNEL_REF, dest)

    # The local write fault is a categorized infrastructure failure, not a raw OSError,
    # and carries the staging op label, the destination, and the operator-facing message.
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value).startswith("failed to write the staged object to the per-Run path")
    assert excinfo.value.details["op"] == "stage"
    assert excinfo.value.details["dest"] == str(dest)
    assert not dest.exists()


def test_install_categorizes_staging_mkdir_failure(tmp_path: Path) -> None:
    # A regular file where the per-System staging dir must be makes mkdir(parents=True) fail
    # with a non-permission OSError (NotADirectoryError) → stays infrastructure_failure.
    (tmp_path / str(_SYS)).write_bytes(b"not-a-dir")
    inst = _install(conn=_conn_with_existing(), staging_root=tmp_path)

    with pytest.raises(CategorizedError) as excinfo:
        inst.install(_request(initrd_ref=_INITRD_REF))

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert excinfo.value.details["op"] == "mkdir"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses the directory-mode write check, so mkdir would not raise",
)
def test_install_unwritable_staging_root_is_config_error(tmp_path: Path) -> None:
    # An unwritable staging root (the #655 symptom: a root-owned parent) makes the per-Run
    # mkdir raise PermissionError. That is operator misconfiguration, not retry-able
    # infrastructure: it must surface as a CONFIGURATION_ERROR naming the env var, the path
    # tried, and an actionable remedy.
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    staging_root.chmod(0o500)  # readable/executable but not writable by the run user
    try:
        inst = _install(conn=_conn_with_existing(), staging_root=staging_root)

        with pytest.raises(CategorizedError) as excinfo:
            inst.install(_request(initrd_ref=_INITRD_REF))
    finally:
        staging_root.chmod(0o700)  # restore so tmp_path cleanup can recurse

    err = excinfo.value
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details["env_var"] == "KDIVE_INSTALL_STAGING"
    # The remedy and the configured staging root are both surfaced for the operator.
    assert str(staging_root) in str(err.details["staging_root"])
    remedy = str(err.details["remedy"])
    assert "writable" in remedy
    assert "virt_image_t" in remedy


# --- live_vm real redefine + boot ----------------------------------------------------


@pytest.mark.live_vm
@pytest.mark.live_vm_provisioned
def test_live_vm_real_install_boot() -> None:  # pragma: no cover - live_vm
    import shutil

    contract = require_live_vm_provisioned()
    if not shutil.which("virsh"):
        pytest.skip("virsh not on PATH; local install-boot needs a local libvirt install")
    # The operator points KDIVE_LIVE_VM_SYSTEM_ID at a System already provisioned + installed
    # with a kdive-ready rootfs (epic #123 build/install harness). boot() power-cycles it and
    # drives the real _real_readiness console probe; a clean kdive-ready boot resolves without
    # raising. The vulnerable-vs-fixed A/B is exercised host-free by the committed crash/clean
    # fixtures (test_*_fixture_classifies_*) and end-to-end by the #123 integration harness.
    booter = LocalLibvirtInstall.from_env()
    booter.boot(UUID(contract.system_id))  # no raise == readiness resolved ok at the marker


# --- classify_console: the pure readiness verdict core (ADR-0055) --------------------

_MARKER = "kdive-ready"


@pytest.mark.parametrize(
    "signature_line",
    [
        "[   22.10] Kernel panic - not syncing: Attempted to kill init!",
        "[   22.10] watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [udevd:142]",
        "[   22.10] Oops: 0000 [#1] PREEMPT SMP",
        "[   22.10] general protection fault: 0000 [#1] SMP",
        "[   22.10] Unable to handle kernel paging request at virtual address 0",
        "[   22.10] BUG: KASAN: slab-out-of-bounds in __d_lookup+0x1a/0x2b",
        "[   22.10] BUG: KFENCE: use-after-free read in d_lookup",
        "[   22.10] UBSAN: shift-out-of-bounds in kernel/foo.c:12:34",
        "[   22.10] rcu: INFO: rcu_sched self-detected stall on CPU",
    ],
)
def test_classify_crash_signatures_resolve_crashed(signature_line: str) -> None:
    data = f"[    0.00] booting\n{signature_line}\n  __d_lookup+0x1a\n".encode()
    assert classify_console(data, marker=_MARKER) == "crashed"


def test_classify_marker_line_alone_is_ready() -> None:
    data = b"[    0.00] booting\n[    3.40] systemd: reached target\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_marker_after_getty_login_prefix_is_ready() -> None:
    # #1266: getty prints `kdive login: ` with no trailing newline, so the readiness unit's
    # marker echoes onto the same line. The marker bytes are present after a token boundary
    # (the space in `login: `) → ready, not a false boot_timeout.
    data = b"[    3.40] systemd: reached target\nkdive login: kdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_marker_after_getty_prefix_without_trailing_newline_is_ready() -> None:
    # The real capture has no trailing newline after the marker on the getty line.
    data = b"[    3.40] systemd: reached target\nkdive login: kdive-ready"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_marker_glued_to_prefix_token_is_pending() -> None:
    # A non-whitespace prefix glued to the marker (no token boundary) must not fire, so
    # `kdive-ready` appearing mid-token elsewhere does not false-positive.
    data = b"[    3.40] fookdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "pending"


def test_classify_getty_prefix_preserves_pre_marker_crash_region() -> None:
    # The crash scan region ends at the marker; a getty-prefixed marker line must not mask a
    # crash that preceded it.
    data = b"[    1.0] Kernel panic - not syncing\nkdive login: kdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "crashed"


def test_classify_empty_is_pending() -> None:
    assert classify_console(b"", marker=_MARKER) == "pending"


def test_classify_no_marker_no_crash_is_pending() -> None:
    data = b"[    0.00] Linux version 7.0.0\n[    1.10] still booting\n"
    assert classify_console(data, marker=_MARKER) == "pending"


def test_classify_debug_substring_is_not_a_crash() -> None:
    # `(?<![A-Za-z])BUG:` must not match `DEBUG:` (no false crash on a benign line).
    data = b"[    1.0] app DEBUG: initializing the readiness subsystem\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_crash_before_marker_wins() -> None:
    data = b"[    1.0] Kernel panic - not syncing\n[    2.0] late\nkdive-ready\n"
    assert classify_console(data, marker=_MARKER) == "crashed"


def test_classify_signature_after_marker_stays_ready() -> None:
    # Pre-marker scoping: a signature *after* the marker line does not flip a healthy boot.
    data = b"kdive-ready\n[    4.0] some-daemon: BUG: benign post-marker chatter\n"
    assert classify_console(data, marker=_MARKER) == "ready"


def test_classify_systemd_unit_line_is_not_the_marker() -> None:
    # Whole-line match: `Starting kdive-ready.service` contains the substring but is not the signal.
    data = b"[    3.2] systemd[1]: Starting kdive-ready.service - KDIVE marker...\n"
    assert classify_console(data, marker=_MARKER) == "pending"


def test_classify_malformed_utf8_does_not_raise() -> None:
    data = b"\xff\xfe partial \x80 bytes, still booting\n"
    assert classify_console(data, marker=_MARKER) == "pending"


# --- first_crash_signature: the shared crash matcher (#984) ---------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("[22.1] Kernel panic - not syncing: Attempted to kill init!", "Kernel panic"),
        ("[22.1] watchdog: BUG: soft lockup - CPU#0 stuck", "BUG:"),
        ("[22.1] Oops: 0000 [#1] PREEMPT SMP", "Oops:"),
        ("[22.1] general protection fault: 0000 [#1] SMP", "general protection fault"),
        ("[22.1] Unable to handle kernel paging request", "Unable to handle kernel"),
        ("[22.1] BUG: KASAN: slab-out-of-bounds in __d_lookup", "BUG:"),
        ("[22.1] BUG: KFENCE: use-after-free read in d_lookup", "BUG:"),
        ("[22.1] UBSAN: shift-out-of-bounds in kernel/foo.c:12:34", "UBSAN:"),
        ("[22.1] rcu: INFO: rcu_sched self-detected stall on CPU", "detected stall"),
    ],
)
def test_first_crash_signature_matches_each_family(line: str, expected: str) -> None:
    match = first_crash_signature(line)
    assert match is not None
    assert match.group(0) == expected


def test_first_crash_signature_none_on_benign_text() -> None:
    assert first_crash_signature("[0.0] Linux version 7.0.0\n[1.1] still booting\n") is None


def test_first_crash_signature_word_boundary_excludes_debug() -> None:
    # `(?<![A-Za-z])BUG:` must not match `DEBUG:` — no false crash on a benign line.
    assert first_crash_signature("app DEBUG: initializing readiness") is None


def test_first_crash_signature_returns_first_of_two() -> None:
    # Deterministic: the earliest match (lowest offset) is returned.
    text = "[1] booting\n[2] Oops: 0000\n[3] Kernel panic - not syncing\n"
    match = first_crash_signature(text)
    assert match is not None
    assert match.group(0) == "Oops:"


_FIXTURES = Path(__file__).parent / "fixtures"


def test_verdict_to_result_crashed_is_answered_failure() -> None:
    # The demo's load-bearing signal: a crashed verdict must resolve to readiness failure.
    assert _verdict_to_result(ConsoleVerdict.CRASHED, exited=False) == ReadinessResult(
        answered=True, ok=False
    )


def test_verdict_to_result_ready_is_answered_ok() -> None:
    assert _verdict_to_result(ConsoleVerdict.READY, exited=False) == ReadinessResult(
        answered=True, ok=True
    )


def test_verdict_to_result_pending_running_keeps_polling() -> None:
    # A still-booting guest is not yet answered → None tells the probe to keep polling.
    assert _verdict_to_result(ConsoleVerdict.PENDING, exited=False) is None


def test_verdict_to_result_pending_exited_is_answered_failure() -> None:
    # A guest that exited without reaching the marker is answered-but-failed (v1's `exited`).
    assert _verdict_to_result(ConsoleVerdict.PENDING, exited=True) == ReadinessResult(
        answered=True, ok=False
    )


def test_real_readiness_treats_missing_domain_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness_mod, "read_console_log", lambda path: b"")
    monkeypatch.setattr(
        readiness_mod,
        "_domain_exit_probe",
        lambda name: readiness_mod._DomainExitProbe(True),
    )

    result = readiness_mod._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is True
    assert result.ok is False


def test_real_readiness_ready_marker_answers_without_probing_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A console that already shows the readiness marker answers ok immediately — the domain
    # exit probe must NOT be consulted (it would be a wasted live-host call), guarding the
    # early-return on a non-None first verdict.
    monkeypatch.setattr(readiness_mod, "read_console_log", lambda path: b"kdive-ready\n")

    def fail_probe(name: str) -> readiness_mod._DomainExitProbe:
        raise AssertionError("exit probe must not run once the console already answered")

    monkeypatch.setattr(readiness_mod, "_domain_exit_probe", fail_probe)

    result = readiness_mod._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is True
    assert result.ok is True


def test_real_readiness_running_guest_stays_unanswered_with_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pending console + a still-running guest: no answer yet, the loop must keep polling, and
    # the probe diagnostic is carried so a later boot timeout can explain itself. ok must be
    # False (not True) for an unanswered probe.
    monkeypatch.setattr(readiness_mod, "read_console_log", lambda path: b"booting...\n")
    monkeypatch.setattr(readiness_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        readiness_mod,
        "_domain_exit_probe",
        lambda name: readiness_mod._DomainExitProbe(False, "virsh hiccup"),
    )

    result = readiness_mod._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is False
    assert result.ok is False
    assert result.probe_error == "virsh hiccup"


def test_real_readiness_reread_after_exit_honors_late_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First read is pending; the guest then exits and a re-read shows the readiness marker that
    # landed just before it stopped — that late verdict (exited=True) must be honored. A success
    # marker yields ok=True, which the generic exited fallback (answered=True, ok=False) can never
    # produce, so this pins the reread call site: dropping the reread would surface ok=False.
    reads = iter([b"booting...\n", b"kdive-ready\n"])
    monkeypatch.setattr(readiness_mod, "read_console_log", lambda path: next(reads))
    monkeypatch.setattr(
        readiness_mod,
        "_domain_exit_probe",
        lambda name: readiness_mod._DomainExitProbe(True),
    )

    result = readiness_mod._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is True
    assert result.ok is True


def test_domain_exited_treats_missing_kdive_domain_as_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def domstate_missing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["virsh"],
            returncode=1,
            stdout="",
            stderr="error: failed to get domain 'kdive-22222222-2222-2222-2222-222222222222'",
        )

    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(readiness_mod.subprocess, "run", domstate_missing)

    assert readiness_mod._domain_exited("kdive-22222222-2222-2222-2222-222222222222") is True


def test_domain_exit_probe_uses_resolved_virsh_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def domstate_running(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="running", stderr="")

    monkeypatch.setattr(readiness_mod.subprocess, "run", domstate_running)

    assert readiness_mod._domain_exited("kdive-22222222-2222-2222-2222-222222222222") is False
    assert calls[0][0] == "/usr/bin/virsh"


def _capture_domstate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stdout: str,
    stderr: str = "",
) -> dict[str, object]:
    """Stub virsh + subprocess.run; return the recorded args/kwargs of the one call."""
    recorded: dict[str, object] = {}
    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(readiness_mod.subprocess, "run", fake_run)
    return recorded


def test_domain_exit_probe_builds_connection_qualified_domstate_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu+tls://probe.example/system")
    recorded = _capture_domstate(monkeypatch, returncode=0, stdout="running")

    readiness_mod._domain_exit_probe("kdive-abc")

    args = recorded["args"]
    assert isinstance(args, list)
    # The probe targets the configured connection URI and the `domstate` subcommand for
    # the named domain; a wrong flag or subcommand would query the wrong thing. Setting the
    # URI explicitly pins that the configured value flows into the argv, independent of the
    # Setting default.
    assert args[1] == "-c"
    assert args[2] == "qemu+tls://probe.example/system"
    assert args[3] == "domstate"
    assert args[4] == "kdive-abc"


def test_domain_exit_probe_runs_bounded_captured_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _capture_domstate(monkeypatch, returncode=0, stdout="running")

    readiness_mod._domain_exit_probe("kdive-abc")

    kwargs = cast("dict[str, object]", recorded["kwargs"])
    assert isinstance(kwargs, dict)
    # Output must be captured and decoded so stdout/stderr parsing works, the probe must be
    # time-bounded so a wedged host cannot hang the boot loop, and check=False so a nonzero
    # exit is inspected here rather than raising.
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == readiness_mod._DOMSTATE_PROBE_TIMEOUT
    assert kwargs["check"] is False


def test_domain_exit_probe_terminal_domstate_is_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_domstate(monkeypatch, returncode=0, stdout="shut off")

    probe = readiness_mod._domain_exit_probe("kdive-abc")

    # A terminal domstate (case-insensitive) means the guest stopped; nothing kept it alive.
    assert probe.exited is True
    assert probe.error is None


def test_domain_exit_probe_running_is_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_domstate(monkeypatch, returncode=0, stdout="running")

    probe = readiness_mod._domain_exit_probe("kdive-abc")

    assert probe.exited is False
    assert probe.error is None


def test_domain_exit_probe_missing_domain_needs_both_prefix_and_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A nonzero exit whose stderr is the libvirt "failed to get domain" signature counts as
    # exited ONLY for a kdive- domain (the names this plane owns).
    _capture_domstate(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="error: failed to get domain 'kdive-abc'",
    )
    assert readiness_mod._domain_exit_probe("kdive-abc").exited is True


def test_domain_exit_probe_missing_signature_for_foreign_name_is_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same "failed to get domain" stderr but a non-kdive domain name: NOT treated as exited
    # (guards the AND between the name prefix and the stderr signature). The probe instead
    # reports the stderr as a bounded probe error.
    _capture_domstate(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="error: failed to get domain 'other-vm'",
    )
    probe = readiness_mod._domain_exit_probe("other-vm")
    assert probe.exited is False
    assert probe.error is not None


def test_domain_exit_probe_kdive_prefix_without_signature_is_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kdive- domain with a nonzero exit but a DIFFERENT stderr is not proof of exit; it is
    # a probe error to keep polling on (guards the AND with the stderr signature).
    _capture_domstate(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="error: connection refused",
    )
    probe = readiness_mod._domain_exit_probe("kdive-abc")
    assert probe.exited is False
    assert probe.error == "error: connection refused"


def test_domain_exit_probe_zero_exit_unknown_state_is_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # returncode 0 with a non-terminal, non-running state (e.g. "paused") is not an exit and
    # carries no probe error (guards the returncode != 0 branch from firing on success).
    _capture_domstate(monkeypatch, returncode=0, stdout="paused")
    probe = readiness_mod._domain_exit_probe("kdive-abc")
    assert probe.exited is False
    assert probe.error is None


def test_real_readiness_reports_domstate_probe_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def domstate_timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["virsh"], timeout=2)

    monkeypatch.setattr(readiness_mod, "read_console_log", lambda path: b"")
    monkeypatch.setattr(readiness_mod.time, "sleep", lambda _: None)
    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(readiness_mod.subprocess, "run", domstate_timeout)

    result = readiness_mod._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is False
    assert result.probe_error == "virsh domstate timed out after 2s"


def test_crash_fixture_classifies_crashed() -> None:
    data = (_FIXTURES / "console_crash_dhash.log").read_bytes()
    assert classify_console(data) == "crashed"


def test_clean_fixture_classifies_ready() -> None:
    data = (_FIXTURES / "console_clean_ready.log").read_bytes()
    assert classify_console(data) == "ready"


# --- TCG boot-window scaling (ADR-0341, #1143) ----------------------------------------------
#
# The booter is built with boot_window_polls=3 (see _install). An always-not-answered readiness
# seam drives the loop to BOOT_TIMEOUT, so the number of readiness probes is the effective
# window. KVM is unscaled (3 probes); TCG/None scale by KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER.


@pytest.fixture
def _tcg_multiplier_5() -> Iterator[None]:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "5.0"})
    yield
    config.reset()


def _boot_expecting_timeout(inst: LocalLibvirtInstall, accel: str | None) -> None:
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS, accel=accel)
    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT


def test_kvm_boot_window_is_unscaled(tmp_path: Path, _tcg_multiplier_5: None) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    _boot_expecting_timeout(inst, "kvm")
    assert seam.calls == 3  # ceil(3 * 1.0) — KVM never scales


def test_tcg_boot_window_scaled_by_multiplier(tmp_path: Path, _tcg_multiplier_5: None) -> None:
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    _boot_expecting_timeout(inst, "tcg")
    assert seam.calls == 15  # ceil(3 * 5.0)


def test_unknown_accel_boot_window_is_scaled(tmp_path: Path, _tcg_multiplier_5: None) -> None:
    # TCG-safe fallback: an unrecorded (None) accel gets the generous scaled window.
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    _boot_expecting_timeout(inst, None)
    assert seam.calls == 15  # ceil(3 * 5.0)


def test_default_boot_accel_is_none_scaled(tmp_path: Path, _tcg_multiplier_5: None) -> None:
    # A caller that omits accel entirely gets the safe (scaled) window, not the KVM window.
    domain = _domain()
    conn = FakeLibvirtConn(lookup={domain.domain_name: domain})
    seam = _Readiness(answered=False)
    inst = _install(conn=conn, seam=seam, staging_root=tmp_path)
    with pytest.raises(CategorizedError) as caught:
        inst.boot(_SYS)
    assert caught.value.category is ErrorCategory.BOOT_TIMEOUT
    assert seam.calls == 15
