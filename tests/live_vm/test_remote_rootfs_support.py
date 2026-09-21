"""Fail-loud environment contract for the supplied ROOTFS carrier."""

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import libvirt
import pytest

from tests.live_vm import remote_rootfs_support as support
from tests.live_vm.remote_rootfs_support import ROOTFS_ENV, require_remote_rootfs


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"QFI\xfb" + b"test source")
    values = {
        ROOTFS_ENV: str(source),
        "KDIVE_LIVE_VM_REMOTE_URI": "qemu+tls://host.example/system",
        "KDIVE_LIVE_VM_REMOTE_BASE_IMAGE": "operator.qcow2",
        "KDIVE_LIVE_VM_REMOTE_GDB_ADDR": "192.0.2.10",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return source


def test_unset_rootfs_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROOTFS_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_remote_rootfs()


@pytest.mark.parametrize(
    "name",
    [
        "KDIVE_LIVE_VM_REMOTE_URI",
        "KDIVE_LIVE_VM_REMOTE_BASE_IMAGE",
        "KDIVE_LIVE_VM_REMOTE_GDB_ADDR",
    ],
)
def test_missing_companion_fails(
    configured: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(pytest.fail.Exception, match=name):
        require_remote_rootfs()


@pytest.mark.parametrize(
    "uri",
    [
        "qemu:///system",
        "qemu+tls:///system",
        "qemu+tls://host.example/session",
        "qemu+tls://host.example/system?no_verify=1",
        "qemu+tls://host.example/system?No%5fVerify=1",
        "qemu+tls://host.example/system?tls_priority=NORMAL",
    ],
)
def test_invalid_transport_fails(
    configured: Path, monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_URI", uri)
    with pytest.raises(pytest.fail.Exception):
        require_remote_rootfs()


@pytest.mark.parametrize("source", ["relative.qcow2", "/absent/rootfs.qcow2"])
def test_missing_or_relative_source_fails(
    configured: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv(ROOTFS_ENV, source)
    with pytest.raises(pytest.fail.Exception, match="absolute readable"):
        require_remote_rootfs()


def test_raw_source_fails(configured: Path) -> None:
    configured.write_bytes(b"raw disk")
    with pytest.raises(pytest.fail.Exception, match="qcow2"):
        require_remote_rootfs()


def test_invalid_gdb_address_fails(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIVE_VM_REMOTE_GDB_ADDR", "host.example")
    with pytest.raises(pytest.fail.Exception):
        require_remote_rootfs()


def test_complete_contract(configured: Path) -> None:
    contract = require_remote_rootfs()
    assert contract.source == configured
    assert contract.base_image == "operator.qcow2"
    assert contract.uri == "qemu+tls://host.example/system"
    assert contract.gdb_addr == "192.0.2.10"


@pytest.mark.parametrize("failure", ["connection", "pool"])
@pytest.mark.parametrize("pytest_failure", [False, True])
def test_cleanup_preserves_primary_and_attempts_independent_domain(
    configured: Path, monkeypatch: pytest.MonkeyPatch, failure: str, pytest_failure: bool
) -> None:
    contract = require_remote_rootfs()
    conn = Mock()
    pool = conn.storagePoolLookupByName.return_value
    pool.storageVolLookupByName.return_value.XMLDesc.return_value = "<volume/>"
    connections = Mock()
    connections.connection.side_effect = (
        [nullcontext(conn), libvirt.libvirtError("cleanup connection unavailable")]
        if failure == "connection"
        else [nullcontext(conn), nullcontext(conn)]
    )
    if failure == "pool":
        conn.storagePoolLookupByName.side_effect = [
            conn.storagePoolLookupByName.return_value,
            libvirt.libvirtError("cleanup pool unavailable"),
            libvirt.libvirtError("cleanup pool unavailable"),
            libvirt.libvirtError("cleanup pool unavailable"),
        ]
    monkeypatch.setattr(support, "_Connections", lambda contract: connections)
    monkeypatch.setattr(support.RootfsCase, "assert_absent", lambda self: None)
    primary = (
        pytest.fail.Exception("original live assertion")
        if pytest_failure
        else AssertionError("original live assertion")
    )
    with pytest.raises(ExceptionGroup) as caught, support.rootfs_case(contract):
        raise primary
    reported = caught.value.exceptions[0]
    assert (reported.__cause__ if pytest_failure else reported) is primary
    assert "cleanup" in str(caught.value.exceptions[1])
    if failure == "pool":
        conn.lookupByName.return_value.destroy.assert_called_once()
        conn.lookupByName.return_value.undefine.assert_called_once()


def test_volume_identity_ignores_only_dynamic_libvirt_bookkeeping() -> None:
    original = (
        "<volume><name>operator.qcow2</name><allocation>10</allocation><physical>10</physical>"
        "<target><path>/pool/operator.qcow2</path><permissions><mode>0644</mode>"
        "<owner>0</owner><group>0</group></permissions>"
        "<timestamps><atime>1</atime><mtime>1</mtime><ctime>1</ctime></timestamps></target></volume>"
    )
    changed = (
        original.replace("<allocation>10", "<allocation>14")
        .replace("<owner>0", "<owner>100")
        .replace("<group>0", "<group>100")
        .replace("<atime>1", "<atime>2")
        .replace("<ctime>1", "<ctime>2")
    )
    assert support.stable_volume_xml(original) == support.stable_volume_xml(changed)
    for old, new in (
        ("operator.qcow2", "replaced.qcow2"),
        ("<physical>10", "<physical>11"),
        ("<mtime>1", "<mtime>2"),
        ("<mode>0644", "<mode>0666"),
    ):
        assert support.stable_volume_xml(original) != support.stable_volume_xml(
            original.replace(old, new)
        )
