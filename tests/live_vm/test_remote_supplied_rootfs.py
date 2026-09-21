"""Real qemu+tls supplied ROOTFS proof for ADR-0440 (#1516)."""

from __future__ import annotations

from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.volume_upload import (
    StreamSource,
    VolumeUploadConn,
    upload_qcow2_volume,
)
from tests.live_vm.remote_rootfs_support import require_remote_rootfs, rootfs_case

pytestmark = [pytest.mark.live_vm, pytest.mark.live_vm_remote]


def test_supplied_rootfs_boots_and_teardown_reclaims(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = require_remote_rootfs()
    uploads: list[str] = []

    def stage(conn: VolumeUploadConn, pool: str, name: str, source: Path) -> None:
        upload_qcow2_volume(conn, pool, name, source)
        uploads.append(name)

    with rootfs_case(contract) as case:
        monkeypatch.setattr(case.provisioner, "_stage_base_volume", stage)
        domain = case.provisioner.provision(case.system_id, case.profile(supplied=True))
        assert domain == case.domain
        assert uploads == [case.volumes[1]]
        case.assert_booted_backing(supplied=True)
        case.provisioner.teardown(case.domain)
        case.assert_absent()


@pytest.mark.parametrize("fault", ["send", "finish"])
def test_partial_upload_fault_reclaims_volume(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = require_remote_rootfs()
    sent = 0
    injected = False
    original_send = libvirt.virStream.sendAll

    def send(stream: libvirt.virStream, handler: StreamSource, opaque: object) -> None:
        def read(active_stream: object, nbytes: int, data: object) -> bytes:
            nonlocal sent, injected
            if fault == "send" and sent:
                injected = True
                raise libvirt.libvirtError("injected ROOTFS partial stream failure")
            chunk = handler(active_stream, nbytes, data)
            sent += len(chunk)
            return chunk

        original_send(stream, read, opaque)

    def finish(stream: libvirt.virStream) -> int:
        nonlocal injected
        assert sent > 0
        injected = True
        raise libvirt.libvirtError("injected ROOTFS finish failure")

    with rootfs_case(contract) as case:
        with monkeypatch.context() as patch:
            patch.setattr(libvirt.virStream, "sendAll", send)
            if fault == "finish":
                patch.setattr(libvirt.virStream, "finish", finish)
            with pytest.raises(CategorizedError) as caught:
                case.provisioner.provision(case.system_id, case.profile(supplied=True))
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert injected and sent > 0
        if fault == "finish":
            assert sent == contract.source.stat().st_size
        case.assert_absent()


@pytest.mark.parametrize("supplied", [True, False], ids=["supplied", "operator"])
def test_failed_provision_reclaims_only_supplied_base(
    supplied: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = require_remote_rootfs()
    injected = False
    original_define = libvirt.virConnect.defineXML

    with rootfs_case(contract) as case:

        def define(conn: libvirt.virConnect, xml: str) -> libvirt.virDomain:
            nonlocal injected
            if case.domain not in xml:
                return original_define(conn, xml)
            pool = conn.storagePoolLookupByName("default")
            pool.storageVolLookupByName(case.volumes[0])
            pool.storageVolLookupByName(case.volumes[1] if supplied else contract.base_image)
            injected = True
            raise libvirt.libvirtError("injected ROOTFS define failure")

        with monkeypatch.context() as patch:
            patch.setattr(libvirt.virConnect, "defineXML", define)
            with pytest.raises(CategorizedError) as caught:
                case.provisioner.provision(case.system_id, case.profile(supplied=supplied))
        assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
        assert injected
        case.assert_absent()


def test_operator_staged_rootfs_provisions_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = require_remote_rootfs()

    def forbid_upload(*args: object) -> None:
        pytest.fail("operator-staged profile attempted an upload")

    with rootfs_case(contract) as case:
        monkeypatch.setattr(case.provisioner, "_stage_base_volume", forbid_upload)
        case.provisioner.provision(case.system_id, case.profile(supplied=False))
        case.assert_booted_backing(supplied=False)
        case.provisioner.teardown(case.domain)
        case.assert_absent()


def test_non_qcow2_rejected_before_volume_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = require_remote_rootfs()
    invalid = tmp_path / "invalid.qcow2"
    invalid.write_bytes(b"not a qcow2")
    created: list[str] = []
    original_create = libvirt.virStoragePool.createXML

    def create(pool: libvirt.virStoragePool, xml: str, flags: int = 0) -> libvirt.virStorageVol:
        created.append(xml)
        return original_create(pool, xml, flags)

    with rootfs_case(contract, allowed_roots=(tmp_path,)) as case:
        with monkeypatch.context() as patch:
            patch.setattr(libvirt.virStoragePool, "createXML", create)
            with pytest.raises(CategorizedError) as caught:
                case.provisioner.provision(
                    case.system_id, case.profile(supplied=True, source=invalid)
                )
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert "not a qcow2" in str(caught.value)
        assert created == []
        case.assert_absent()
