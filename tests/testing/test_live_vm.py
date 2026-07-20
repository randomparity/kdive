"""Unit tests for the pytest-free live_vm harness mechanism (kdive.testing.live_vm)."""

from __future__ import annotations

import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError
from kdive.testing.live_vm import (
    LiveVmBootTimeout,
    boot_preserved_gdbstub_domain,
    boot_throwaway_domain,
    prepare_session_runtime,
    throwaway_domain_xml,
    wait_for_active,
    wait_for_panic,
    wait_for_ssh,
)


def _fake_libvirt_module() -> types.SimpleNamespace:
    """A stub ``libvirt`` module for the teardown branch: its libvirtError + undefine flag constant.

    boot_throwaway_domain imports libvirt lazily *inside its finally* (only when a domain was
    defined), so a boot unit test injects this via ``monkeypatch.setitem(sys.modules, "libvirt",
    ...)`` and the teardown runs without real libvirt — proving the fake path and letting the boot
    tests pass on a libvirt-less host.
    """
    mod = types.SimpleNamespace()
    mod.libvirtError = Exception
    mod.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA = 1
    return mod


class _FakeDomain:
    def __init__(self, *, boot_succeeds: bool = True) -> None:
        # boot_succeeds models whether create() actually brings the domain up. The timeout test
        # sets it False so create() does NOT flip active True — otherwise create() would override
        # the 'never active' intent and wait_for_active would return True.
        self.boot_succeeds = boot_succeeds
        self.active = False
        self.destroyed = False
        self.undefined = False

    def isActive(self) -> bool:  # noqa: N802 - libvirt name
        return self.active

    def create(self) -> None:
        if self.boot_succeeds:
            self.active = True

    def destroy(self) -> None:
        self.destroyed = True
        self.active = False

    def undefineFlags(self, _flags: int) -> None:  # noqa: N802 - libvirt name
        self.undefined = True


class _FakeConn:
    def __init__(self, *, boot_succeeds: bool = True) -> None:
        self.domain = _FakeDomain(boot_succeeds=boot_succeeds)
        self.define_calls = 0
        self.closed = False

    def defineXML(self, _xml: str) -> _FakeDomain:  # noqa: N802 - libvirt name
        self.define_calls += 1
        return self.domain

    def close(self) -> int:
        self.closed = True
        return 0


def _fake_conn_factory(conn: _FakeConn):
    return lambda _uri: conn


def _write_overlay(_base: Path, dest: Path) -> None:
    dest.write_bytes(b"overlay")


def test_boot_yields_and_tears_down_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeConn()
    overlays: list[Path] = []
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")

    def fake_overlay(_base: Path, dest: Path) -> None:
        dest.write_bytes(b"overlay")
        overlays.append(dest)

    with boot_throwaway_domain(
        rootfs,
        arch="x86_64",
        name="kdive-t",
        mode="qemu:///system",
        _connect=_fake_conn_factory(conn),
        _overlay=fake_overlay,
    ) as live:
        assert live.name == "kdive-t"
        assert conn.define_calls == 1
        assert overlays and overlays[0].exists()
    assert conn.domain.destroyed and conn.domain.undefined and conn.closed
    assert not overlays[0].exists()  # overlay unlinked


def test_boot_raises_before_define_on_ssh_without_port(tmp_path: Path) -> None:
    # No libvirt stub: the precondition must raise BEFORE the try body (no overlay, no connect,
    # no libvirt import), so this passes on a libvirt-less host.
    conn = _FakeConn()
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with (
        pytest.raises(CategorizedError),
        boot_throwaway_domain(
            rootfs,
            arch="x86_64",
            name="k",
            wait_for="ssh",
            _connect=_fake_conn_factory(conn),
            _overlay=_write_overlay,
        ),
    ):
        pass
    assert conn.define_calls == 0  # failed the precondition before defining


def test_boot_timeout_raises_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeConn(boot_succeeds=False)  # create() leaves it inactive → never reaches condition
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with (
        pytest.raises(LiveVmBootTimeout),
        boot_throwaway_domain(
            rootfs,
            arch="x86_64",
            name="k",
            wait_timeout_s=-1.0,
            _connect=_fake_conn_factory(conn),
            _overlay=_write_overlay,
        ),
    ):
        pass
    assert conn.closed  # teardown still ran


def test_boot_refuses_to_clobber_a_pre_existing_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeConn()
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    # A real file already sits at the overlay path — the boot must refuse and NOT delete it.
    collision = rootfs.with_name("k.qcow2")
    collision.write_bytes(b"precious")
    with (
        pytest.raises(CategorizedError),
        boot_throwaway_domain(
            rootfs,
            arch="x86_64",
            name="k",
            _connect=_fake_conn_factory(conn),
            _overlay=_write_overlay,
        ),
    ):
        pass
    assert conn.define_calls == 0  # refused before defining
    assert collision.read_bytes() == b"precious"  # the pre-existing file is untouched


def test_boot_session_mode_restores_xdg_even_on_body_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    monkeypatch.setenv("XDG_CONFIG_HOME", "/original")
    conn = _FakeConn()
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"qcow2")
    with (
        pytest.raises(RuntimeError),
        boot_throwaway_domain(
            rootfs,
            arch="x86_64",
            name="k",
            mode="qemu:///session",
            _connect=_fake_conn_factory(conn),
            _overlay=_write_overlay,
        ),
    ):
        raise RuntimeError("body boom")
    assert os.environ["XDG_CONFIG_HOME"] == "/original"


class _FakeTransientDomain:
    def __init__(self) -> None:
        self.active = True  # createXML returns an already-running transient domain
        self.destroyed = False

    def isActive(self) -> bool:  # noqa: N802 - libvirt name
        return self.active

    def destroy(self) -> None:
        self.destroyed = True
        self.active = False


class _FakeTransientConn:
    def __init__(self) -> None:
        self.domain = _FakeTransientDomain()
        self.create_calls: list[str] = []
        self.closed = False

    def createXML(self, xml: str, _flags: int) -> _FakeTransientDomain:  # noqa: N802 - libvirt name
        self.create_calls.append(xml)
        return self.domain

    def close(self) -> int:
        self.closed = True
        return 0


_GDBSTUB_XML = "<domain type='kvm'><name>kdive-x</name></domain>"


def test_preserved_boot_yields_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeTransientConn()
    console = tmp_path / "console.log"
    console.write_text("Kernel panic - not syncing\n")  # already panicked → wait returns at once
    with boot_preserved_gdbstub_domain(
        _GDBSTUB_XML,
        uri="qemu:///session",
        console_log=console,
        _connect=lambda _uri: conn,
    ) as live:
        assert live.name == "kdive-x"  # extracted from the caller's rendered XML
        assert live.console_log == console
        assert conn.create_calls == [_GDBSTUB_XML]  # booted the caller's XML verbatim
    assert conn.domain.destroyed and conn.closed  # transient teardown ran


def test_preserved_boot_timeout_raises_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeTransientConn()
    console = tmp_path / "console.log"
    console.write_text("booting, no panic\n")
    with (
        pytest.raises(LiveVmBootTimeout),
        boot_preserved_gdbstub_domain(
            _GDBSTUB_XML,
            uri="qemu:///session",
            console_log=console,
            wait_timeout_s=-1.0,  # already past the deadline → the panic-wait fails immediately
            _connect=lambda _uri: conn,
        ),
    ):
        pass
    assert conn.domain.destroyed and conn.closed  # teardown still ran on the boot-timeout path


def test_preserved_boot_session_mode_restores_xdg_even_on_body_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    monkeypatch.setenv("XDG_CONFIG_HOME", "/original")
    conn = _FakeTransientConn()
    console = tmp_path / "console.log"
    console.write_text("Kernel panic - not syncing\n")
    with (
        pytest.raises(RuntimeError),
        boot_preserved_gdbstub_domain(
            _GDBSTUB_XML,
            uri="qemu:///session",
            console_log=console,
            _connect=lambda _uri: conn,
        ),
    ):
        raise RuntimeError("body boom")
    assert os.environ["XDG_CONFIG_HOME"] == "/original"  # #1323 redirect restored on teardown


def test_preserved_boot_unnamed_xml_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "libvirt", _fake_libvirt_module())
    conn = _FakeTransientConn()
    console = tmp_path / "console.log"
    console.write_text("Kernel panic - not syncing\n")
    with boot_preserved_gdbstub_domain(
        "<domain type='kvm'></domain>",  # no <name> → observability fallback, not a boot error
        uri="qemu:///session",
        console_log=console,
        _connect=lambda _uri: conn,
    ) as live:
        assert live.name == "<unnamed>"


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)  # noqa: S314 - kdive-rendered, trusted


def test_builder_x86_emits_q35_ttys0_hostpassthrough_acpi() -> None:
    root = _root(throwaway_domain_xml(name="kdive-x", arch="x86_64", disk_path="/d.qcow2"))
    assert root.get("type") == "kvm"
    os_type = root.find("./os/type")
    assert os_type is not None and os_type.get("machine") == "q35"
    cpu = root.find("./cpu")
    assert cpu is not None and cpu.get("mode") == "host-passthrough"
    assert root.find("./features/acpi") is not None
    assert root.find("./features/vmcoreinfo") is not None
    assert root.find("./devices/serial") is not None
    assert root.find("./devices/console") is not None


def test_builder_ppc64le_emits_pseries_hostmodel_no_acpi() -> None:
    root = _root(throwaway_domain_xml(name="kdive-p", arch="ppc64le", disk_path="/d.qcow2"))
    os_type = root.find("./os/type")
    assert os_type is not None and os_type.get("machine") == "pseries"
    cpu = root.find("./cpu")
    assert cpu is not None and cpu.get("mode") == "host-model"
    assert root.find("./features") is None
    assert root.find("./devices/serial") is not None


def test_builder_serial_log_sink_only_when_console_log_set(tmp_path: Path) -> None:
    without = _root(throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2"))
    assert without.find("./devices/serial/log") is None
    console = tmp_path / "c.log"
    with_log = _root(
        throwaway_domain_xml(name="b", arch="x86_64", disk_path="/d.qcow2", console_log=console)
    )
    log_el = with_log.find("./devices/serial/log")
    assert log_el is not None and log_el.get("file") == str(console)


def test_builder_ssh_netdev_present_iff_port_set() -> None:
    without = throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2")
    assert "hostfwd" not in without
    with_fwd = throwaway_domain_xml(
        name="b", arch="x86_64", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "hostfwd=tcp:127.0.0.1:2222-:22" in with_fwd
    assert "addr=0x10" in with_fwd  # q35 pins the slot
    ppc = throwaway_domain_xml(
        name="c", arch="ppc64le", disk_path="/d.qcow2", ssh_hostfwd_port=2222
    )
    assert "addr=0x10" not in ppc  # pseries does not


def test_builder_direct_kernel_and_default_console_cmdline(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"k")
    root = _root(
        throwaway_domain_xml(name="a", arch="x86_64", disk_path="/d.qcow2", kernel_path=kernel)
    )
    kernel_el = root.find("./os/kernel")
    cmdline_el = root.find("./os/cmdline")
    assert kernel_el is not None and kernel_el.text == str(kernel)
    assert cmdline_el is not None and cmdline_el.text == "root=/dev/vda console=ttyS0 rw"


def test_builder_unknown_arch_raises_configuration_error() -> None:
    with pytest.raises(CategorizedError):
        throwaway_domain_xml(name="a", arch="riscv64", disk_path="/d.qcow2")


def test_wait_for_active_returns_true_when_domain_active() -> None:
    class _Dom:
        def isActive(self) -> bool:  # noqa: N802 - libvirt name
            return True

    assert wait_for_active(_Dom(), deadline_s=1.0) is True


def test_wait_for_panic_true_after_marker_appears(tmp_path: Path) -> None:
    console = tmp_path / "c.log"
    console.write_text("booting...\n")
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            console.write_text("booting...\nKernel panic - not syncing\n")

    assert wait_for_panic(console, deadline_s=100.0, sleep=fake_sleep) is True


def test_wait_for_panic_false_at_deadline(tmp_path: Path) -> None:
    console = tmp_path / "c.log"
    console.write_text("no panic here\n")
    assert wait_for_panic(console, deadline_s=-1.0) is False  # already past deadline


def test_wait_for_panic_waits_through_a_not_yet_created_console(tmp_path: Path) -> None:
    console = tmp_path / "c.log"  # does not exist yet
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            console.write_text("Kernel panic - not syncing\n")

    # A missing file must be treated as "not yet panicked" (keep polling), not FileNotFoundError.
    assert wait_for_panic(console, deadline_s=100.0, sleep=fake_sleep) is True


def test_wait_for_ssh_true_when_probe_eventually_succeeds() -> None:
    seq = iter([False, False, True])

    def probe(_host: str, _port: int) -> bool:
        return next(seq)

    assert (
        wait_for_ssh("127.0.0.1", 2222, deadline_s=100.0, probe=probe, sleep=lambda _s: None)
        is True
    )


def test_wait_for_ssh_false_at_deadline_when_probe_never_succeeds() -> None:
    def probe(_host: str, _port: int) -> bool:
        return False

    assert wait_for_ssh("127.0.0.1", 2222, deadline_s=-1.0, probe=probe) is False


def test_wait_for_ssh_survives_probe_oserror() -> None:
    calls = {"n": 0}

    def probe(_host: str, _port: int) -> bool:
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("connection refused")
        return True

    assert (
        wait_for_ssh("127.0.0.1", 2222, deadline_s=100.0, probe=probe, sleep=lambda _s: None)
        is True
    )


def test_prepare_session_runtime_none_for_system_mode() -> None:
    assert prepare_session_runtime("qemu:///system") is None


def test_prepare_session_runtime_sets_short_xdg_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    monkeypatch.setenv("XDG_CONFIG_HOME", "/original")
    runtime = prepare_session_runtime("qemu:///session")
    assert runtime is not None
    short = os.environ["XDG_CONFIG_HOME"]
    assert short != "/original" and len(short) < 40
    runtime.restore()
    assert os.environ["XDG_CONFIG_HOME"] == "/original"
    assert not Path(short).exists()
