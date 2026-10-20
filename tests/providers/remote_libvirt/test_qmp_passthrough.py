"""Remote QMP passthrough against a real ``virDomain`` (#3294).

``libvirt.virDomain`` has no ``qemuMonitorCommand`` method; the passthrough is the module function
``libvirt_qemu.qemuMonitorCommand(domain, cmd, flags)``. libvirt's built-in ``test:///default``
driver hands back real ``virDomain`` objects and refuses the monitor call with a ``libvirtError``,
so each remote caller must surface its categorized failure here rather than an ``AttributeError``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import OrphanedCapture
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.connection.monitor import qemu_monitor_command
from kdive.providers.remote_libvirt.connection.transport import (
    ClosableConn,
    open_libvirt_protocol,
)
from kdive.providers.remote_libvirt.connection.transport_reset import RemoteLibvirtTransportResetter
from kdive.providers.remote_libvirt.lifecycle.traffic_capture import RemoteLibvirtTrafficCapture
from kdive.providers.remote_libvirt.reaping.capture import RemoteLibvirtCaptureReaper
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend

_TEST_DOMAIN = "test"  # the one domain test:///default defines
_GDB_ADDR = "10.0.0.5"


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        gdb_addr=_GDB_ADDR,
    )


def _open_test_driver[C: ClosableConn](uri: str) -> C:
    """The production opener, pointed at libvirt's built-in test driver."""
    del uri
    return open_libvirt_protocol("test:///default")


def test_qemu_monitor_command_hands_the_domain_to_the_module_function() -> None:
    conn = libvirt.open("test:///default")
    try:
        domain = conn.lookupByName(_TEST_DOMAIN)
        with pytest.raises(libvirt.libvirtError) as exc:
            qemu_monitor_command(domain, '{"execute": "query-status"}', 0)
    finally:
        conn.close()
    # The test driver has no QEMU monitor: reaching it at all proves the call shape.
    assert exc.value.get_error_code() == libvirt.VIR_ERR_NO_SUPPORT


def test_capture_detach_reaches_the_monitor_on_a_real_domain(tmp_path: Path) -> None:
    capturer = RemoteLibvirtTrafficCapture(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=_open_test_driver,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )
    with pytest.raises(CategorizedError) as exc:
        capturer.detach(_TEST_DOMAIN, qom_id="kdive-dump-job")
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert isinstance(exc.value.__cause__, libvirt.libvirtError)


def test_capture_reaper_reaches_the_monitor_on_a_real_domain(tmp_path: Path) -> None:
    reaper = RemoteLibvirtCaptureReaper(
        secret_registry=SecretRegistry(),
        config_for_resource=lambda name: _config(),
        open_connection=_open_test_driver,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )
    capture = OrphanedCapture(
        provider_kind="remote-libvirt",
        resource_id=UUID(int=3),
        resource_name="builder",
        system_id=UUID(int=1),
        domain_name=_TEST_DOMAIN,
        job_id=UUID(int=2),
    )
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(reaper.reclaim_capture(capture))
    assert exc.value.category is ErrorCategory.CONTROL_FAILURE
    assert isinstance(exc.value.__cause__, libvirt.libvirtError)


def test_transport_reset_reaches_the_monitor_on_a_real_domain(tmp_path: Path) -> None:
    resetter = RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        configs_factory=lambda: [_config()],
        open_connection=_open_test_driver,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )
    with pytest.raises(CategorizedError) as exc:
        asyncio.run(
            resetter.reset(
                transport="gdbstub",
                transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
                domain_name=_TEST_DOMAIN,
            )
        )
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert isinstance(exc.value.__cause__, libvirt.libvirtError)
