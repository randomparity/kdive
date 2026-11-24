"""Unit tests for the kdump-completion poll loop (ADR-0217).

``_real_wait_for_vmcore`` used to force-off the domain immediately, racing the in-guest kdump
and harvesting an empty ``/var/crash``. The fix waits for the guest to self-shut-off (kdump's
``final_action poweroff``) within a bounded window before forcing off and harvesting. The wait
is driven through an injected domain-settled probe and sleep seam so the poll logic is unit
tested with fakes. Native boundary failures use injected libvirt connections; real VM
capture remains ``live_vm``-gated.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import Mock
from uuid import UUID, uuid4

import libvirt
import pytest

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve import guestfs, host_dump_capture
from kdive.providers.local_libvirt.retrieve.guestfs import (
    _KDUMP_SETTLE_POLL_INTERVAL_S,
    _KDUMP_SETTLE_TIMEOUT_S,
    _poll_until_settled,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn, libvirt_error


def test_returns_true_without_sleeping_when_already_settled() -> None:
    slept: list[float] = []
    settled = _poll_until_settled(
        lambda: True,
        slept.append,
        timeout_s=120.0,
        poll_interval_s=3.0,
    )
    assert settled is True
    assert slept == []  # an already-settled domain is harvested immediately, no premature wait


def test_returns_true_once_domain_settles_after_some_polls() -> None:
    """The guest is still dumping for a few polls, then self-shuts-off → wait succeeds."""
    probe_results = iter([False, False, False, True])
    slept: list[float] = []

    def is_settled() -> bool:
        return next(probe_results)

    settled = _poll_until_settled(
        is_settled,
        slept.append,
        timeout_s=120.0,
        poll_interval_s=3.0,
    )
    assert settled is True
    # slept exactly once between each of the three not-settled probes and the settling probe.
    assert slept == [3.0, 3.0, 3.0]


def test_times_out_when_domain_never_settles() -> None:
    """A guest whose kdump reboots (never self-shuts-off) → bounded timeout, not unbounded."""
    probes = 0
    slept: list[float] = []

    def is_settled() -> bool:
        nonlocal probes
        probes += 1
        return False

    settled = _poll_until_settled(
        is_settled,
        slept.append,
        timeout_s=12.0,
        poll_interval_s=3.0,
    )
    assert settled is False
    # ceil(12/3) == 4 probes, with a sleep between each pair (one fewer than probes).
    assert probes == 4
    assert slept == [3.0, 3.0, 3.0]


def test_total_wait_is_bounded_by_the_timeout() -> None:
    """Even with sub-interval timeouts the probe budget stays >= 1 and never over-waits."""
    slept: list[float] = []
    settled = _poll_until_settled(
        lambda: False,
        slept.append,
        timeout_s=1.0,
        poll_interval_s=3.0,
    )
    assert settled is False
    assert slept == []  # ceil(1/3) == 1 probe, no sleep, total wait 0s <= timeout


def test_settle_window_constants_are_sane() -> None:
    assert _KDUMP_SETTLE_TIMEOUT_S > _KDUMP_SETTLE_POLL_INTERVAL_S > 0


_DOMAIN_PROBES = [
    (guestfs._real_domain_settled, True, "check_domain_settled"),
    (guestfs._force_off_domain, None, "force_off_domain"),
    (host_dump_capture._real_host_dump_capture, None, "host_dump_capture"),
]


@pytest.fixture
def retrieve_uri() -> Iterator[str]:
    uri = "qemu:///session"
    config.load({"KDIVE_LIBVIRT_URI": uri})
    yield uri
    config.reset()


@pytest.mark.parametrize(("probe", "absent", "operation"), _DOMAIN_PROBES)
@pytest.mark.parametrize("code", [libvirt.VIR_ERR_NO_DOMAIN, libvirt.VIR_ERR_RPC, None])
@pytest.mark.parametrize("close_fails", [False, True])
def test_native_retrieve_domain_boundary(
    probe: Callable[[UUID], object],
    absent: bool | None,
    operation: str,
    code: int | None,
    close_fails: bool,
    retrieve_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    system_id = uuid4()
    fault = libvirt_error(code if code is not None else libvirt.VIR_ERR_INTERNAL_ERROR)
    if code is None:
        fault.err = None
    fault.args = ("private lookup diagnostic",)
    close_fault = libvirt_error(libvirt.VIR_ERR_RPC)
    close_fault.args = ("private close diagnostic",)
    connection = FakeLibvirtConn()
    opened: list[str] = []
    closed: list[bool] = []

    def lookup(_name: str) -> NoReturn:
        raise fault

    def close() -> int:
        closed.append(True)
        if close_fails:
            raise close_fault
        return 0

    def connect(uri: str) -> FakeLibvirtConn:
        opened.append(uri)
        return connection

    monkeypatch.setattr(connection, "lookupByName", lookup)
    monkeypatch.setattr(connection, "close", close)
    monkeypatch.setattr(libvirt, "open", connect)
    if code == libvirt.VIR_ERR_NO_DOMAIN:
        assert probe(system_id) is absent
    else:
        with pytest.raises(CategorizedError) as caught:
            probe(system_id)
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.__cause__ is fault
        assert caught.value.details["system_id"] == str(system_id)
        assert caught.value.details["operation"] == operation
        assert "private lookup diagnostic" not in str(caught.value)
    assert opened == [retrieve_uri]
    assert closed == [True]
    assert "private close diagnostic" not in caplog.text
    if close_fails:
        assert any(r.levelname == "WARNING" and operation in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("probe", "operation"), [(probe, operation) for probe, _absent, operation in _DOMAIN_PROBES]
)
def test_native_retrieve_open_failures(
    probe: Callable[[UUID], object], operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fault = libvirt_error(libvirt.VIR_ERR_RPC)
    system_id = uuid4()

    def connect(_uri: str) -> NoReturn:
        raise fault

    monkeypatch.setattr(libvirt, "open", connect)
    with pytest.raises(CategorizedError) as caught:
        probe(system_id)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.__cause__ is fault
    assert caught.value.details["system_id"] == str(system_id)
    assert caught.value.details["operation"] == operation


@pytest.mark.parametrize(
    ("probe", "method", "operation"),
    [
        (guestfs._real_domain_settled, "state", "check_domain_settled"),
        (guestfs._force_off_domain, "isActive", "force_off_domain"),
        (guestfs._force_off_domain, "destroy", "force_off_domain"),
        (host_dump_capture._real_host_dump_capture, "isActive", "host_dump_capture"),
    ],
)
@pytest.mark.parametrize("code", [libvirt.VIR_ERR_RPC, libvirt.VIR_ERR_NO_DOMAIN])
def test_native_retrieve_operation_failures(
    probe: Callable[[UUID], object],
    method: str,
    operation: str,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_id = uuid4()
    name = domain_name_for(system_id)
    domain = FakeDomain(name, str(system_id), active=True)
    connection = FakeLibvirtConn(lookup={name: domain})
    fault = libvirt_error(code)

    def fail() -> NoReturn:
        raise fault

    monkeypatch.setattr(domain, method, fail)
    monkeypatch.setattr(libvirt, "open", lambda _uri: connection)
    with pytest.raises(CategorizedError) as caught:
        probe(system_id)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.__cause__ is fault
    assert caught.value.details["operation"] == operation


@pytest.mark.parametrize("probe", [probe for probe, _absent, _operation in _DOMAIN_PROBES])
def test_native_retrieve_preserves_programming_failure(
    probe: Callable[[UUID], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fault = RuntimeError("operation bug")
    connection = FakeLibvirtConn()

    def lookup(_name: str) -> NoReturn:
        raise fault

    def close() -> NoReturn:
        raise RuntimeError("cleanup bug")

    monkeypatch.setattr(connection, "lookupByName", lookup)
    monkeypatch.setattr(connection, "close", close)
    monkeypatch.setattr(libvirt, "open", lambda _uri: connection)
    with pytest.raises(RuntimeError) as caught:
        probe(uuid4())
    assert caught.value is fault


@pytest.mark.parametrize(
    ("probe", "active", "expected", "calls"),
    [
        (guestfs._real_domain_settled, False, True, []),
        (guestfs._real_domain_settled, True, False, []),
        (guestfs._force_off_domain, False, None, []),
        (guestfs._force_off_domain, True, None, ["destroy"]),
    ],
)
def test_native_retrieve_valid_domain_behavior(
    probe: Callable[[UUID], object],
    active: bool,
    expected: bool | None,
    calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_id = uuid4()
    name = domain_name_for(system_id)
    domain = FakeDomain(name, str(system_id), active=active)
    connection = FakeLibvirtConn(lookup={name: domain})
    monkeypatch.setattr(libvirt, "open", lambda _uri: connection)
    assert probe(system_id) is expected
    assert domain.calls == calls


@pytest.mark.parametrize("failure_phase", ["settle", "force_off"])
def test_harvest_stops_on_native_failure(
    failure_phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    name = domain_name_for(system_id)
    settled = FakeLibvirtConn(lookup={name: FakeDomain(name, str(system_id))})
    broken = FakeLibvirtConn()
    fault = libvirt_error(libvirt.VIR_ERR_RPC)

    def lookup(_name: str) -> NoReturn:
        raise fault

    monkeypatch.setattr(broken, "lookupByName", lookup)
    connections = iter([broken, settled] if failure_phase == "settle" else [settled, broken])
    monkeypatch.setattr(libvirt, "open", lambda _uri: next(connections))
    spool = tmp_path / "spool"
    spool.mkdir()

    def spool_directory(*, prefix: str) -> str:
        return str(spool)

    reader = Mock(side_effect=AssertionError("overlay reader reached"))
    monkeypatch.setattr(guestfs.tempfile, "mkdtemp", spool_directory)
    monkeypatch.setattr(guestfs, "_LibguestfsCoreReader", reader)
    with pytest.raises(CategorizedError) as caught:
        guestfs._real_wait_for_vmcore(system_id)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.__cause__ is fault
    reader.assert_not_called()


@pytest.mark.parametrize(
    ("phase", "fault_type"),
    [
        ("launch", OSError),
        ("mount", OSError),
        ("list", OSError),
        ("download", OSError),
        ("binding", OSError),
        ("constructor", OSError),
        ("constructor", RuntimeError),
    ],
)
@pytest.mark.parametrize("close_fails", [False, True])
def test_failed_capture_releases_spool_and_preserves_reader_error(
    phase: str,
    fault_type: type[Exception],
    close_fails: bool,
    tmp_path: Path,
    retrieve_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = Mock(spec=guestfs._GuestfsHandle)
    fault = fault_type("guest storage unavailable")
    sdk.inspect_os.return_value = ["/dev/root"]
    sdk.inspect_get_mountpoints.return_value = {"/": "/dev/root", "/var": "/dev/var"}
    sdk.glob_expand.side_effect = lambda pattern: (
        ["/var/crash/boot/vmcore"] if pattern == guestfs._VAR_CRASH_GLOB else []
    )
    sdk.statns.return_value = {"st_mtime_sec": 1, "st_size": 4}
    if close_fails:
        sdk.close.side_effect = RuntimeError("cleanup unavailable")

    def mount(_device: str, mountpoint: str) -> None:
        if phase == "mount" and mountpoint == "/var":
            raise fault

    def download(_path: str, dest: str) -> None:
        Path(dest).write_bytes(b"partial")
        if phase == "download":
            raise fault

    sdk.mount_ro.side_effect = mount
    sdk.download.side_effect = download
    if phase == "launch":
        sdk.launch.side_effect = fault
    if phase == "list":
        sdk.glob_expand.side_effect = fault
    factory = Mock(return_value=sdk)
    if phase == "constructor":
        factory.side_effect = fault
    monkeypatch.setitem(
        sys.modules,
        "guestfs",
        None if phase == "binding" else SimpleNamespace(GuestFS=factory),
    )
    monkeypatch.setattr(libvirt, "open", lambda _uri: FakeLibvirtConn())
    monkeypatch.setattr(guestfs, "overlay_path", lambda _system: "overlay.qcow2")
    spool = tmp_path / "spool"

    def allocate(*, prefix: str) -> str:
        assert prefix == "kdive-kdump-"
        spool.mkdir()
        return str(spool)

    monkeypatch.setattr(guestfs.tempfile, "mkdtemp", allocate)
    with pytest.raises(CategorizedError) as caught:
        guestfs._real_wait_for_vmcore(uuid4())
    assert not spool.exists()
    if phase == "binding":
        assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
        assert isinstance(caught.value.__cause__, ImportError)
        sdk.close.assert_not_called()
    else:
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert caught.value.__cause__ is fault
        assert caught.value.details == {"overlay": "overlay.qcow2", "error": fault_type.__name__}
        if phase == "constructor":
            sdk.close.assert_not_called()
        else:
            sdk.close.assert_called_once_with()


@pytest.mark.parametrize("capture", ["vmcore", "vmcore-incomplete", None])
def test_coordinator_hands_off_only_complete_core_spool(
    capture: str | None, tmp_path: Path, retrieve_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = Mock(spec=guestfs._GuestfsHandle)
    sdk.inspect_os.return_value = ["/dev/root"]
    sdk.inspect_get_mountpoints.return_value = {"/": "/dev/root"}
    paths = [] if capture is None else [f"/var/crash/boot/{capture}"]
    sdk.glob_expand.side_effect = lambda pattern: [
        path for path in paths if path.endswith(pattern.rsplit("/", 1)[1])
    ]
    sdk.statns.return_value = {"st_mtime_sec": 1, "st_size": 4}
    sdk.download.side_effect = lambda _path, dest: Path(dest).write_bytes(b"CORE")
    monkeypatch.setitem(sys.modules, "guestfs", SimpleNamespace(GuestFS=lambda **_kwargs: sdk))
    monkeypatch.setattr(libvirt, "open", lambda _uri: FakeLibvirtConn())
    monkeypatch.setattr(guestfs, "overlay_path", lambda _system: "overlay.qcow2")
    spool = tmp_path / "spool"

    def allocate(*, prefix: str) -> str:
        spool.mkdir()
        return str(spool)

    monkeypatch.setattr(guestfs.tempfile, "mkdtemp", allocate)
    outcome = guestfs._real_wait_for_vmcore(uuid4())
    assert outcome.core == (spool / "vmcore" if capture == "vmcore" else None)
    assert outcome.incomplete_found is (capture == "vmcore-incomplete")
    assert spool.exists() is (capture == "vmcore")
    if outcome.core is not None:
        assert outcome.core.read_bytes() == b"CORE"
        sdk.download.assert_called_once_with("/var/crash/boot/vmcore", str(outcome.core))
    else:
        sdk.download.assert_not_called()
    sdk.close.assert_called_once_with()


@pytest.mark.parametrize(
    "fault_type", [KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError]
)
def test_guestfs_constructor_preserves_control_exceptions(
    fault_type: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    fault = fault_type("constructor interrupted")
    factory = Mock(side_effect=fault)
    monkeypatch.setitem(sys.modules, "guestfs", SimpleNamespace(GuestFS=factory))
    with pytest.raises(fault_type) as caught:
        guestfs._LibguestfsCoreReader("overlay.qcow2")
    assert caught.value is fault
    factory.assert_called_once_with(python_return_dict=True)
