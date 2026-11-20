"""Local-libvirt hot-plug seams: scratch-disk files and the NIC source (#3687, ADR-0823)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.lifecycle import hotplug
from kdive.providers.local_libvirt.lifecycle.hotplug import LocalDeviceVolumes, scratch_disk_prefix
from kdive.providers.ports.lifecycle import HotplugDeviceKind
from kdive.providers.shared.libvirt_hotplug import LibvirtDeviceHotplug

DOMAIN = "kdive-11111111-1111-1111-1111-111111111111"


def _fake_create(created: list[tuple[str, int]]):  # noqa: ANN202
    def create(path: str, size_gb: int) -> None:
        created.append((path, size_gb))
        Path(path).write_bytes(b"qcow2")

    return create


def test_create_places_the_disk_beside_the_domain_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[tuple[str, int]] = []
    monkeypatch.setattr(hotplug, "create_scratch_qcow2", _fake_create(created))
    device_id = str(uuid4())

    path = LocalDeviceVolumes(str(tmp_path)).create(None, DOMAIN, device_id, 3)

    assert path == str(tmp_path / f"{scratch_disk_prefix(DOMAIN)}{device_id}.qcow2")
    assert created == [(path, 3)]


def test_remove_is_idempotent_and_remove_all_spares_other_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hotplug, "create_scratch_qcow2", _fake_create([]))
    volumes = LocalDeviceVolumes(str(tmp_path))
    ours = [volumes.create(None, DOMAIN, str(uuid4()), 1) for _ in range(2)]
    other = volumes.create(None, "kdive-other", str(uuid4()), 1)
    overlay = tmp_path / "11111111-1111-1111-1111-111111111111-overlay.qcow2"
    overlay.write_bytes(b"boot")

    volumes.remove(None, DOMAIN, str(uuid4()))  # absent: no error
    volumes.remove_all(None, DOMAIN)

    assert not any(Path(p).exists() for p in ours)
    assert Path(other).exists()
    assert overlay.exists()


def test_nic_source_is_user_mode_unless_a_network_is_named() -> None:
    assert hotplug._nic_source(None) == ("user", None)  # noqa: SLF001
    assert hotplug._nic_source("lab") == ("network", "lab")  # noqa: SLF001


class _AbsentDomain:
    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return "<domain><devices/></domain>"


class _Conn:
    def lookupByName(self, name: str) -> _AbsentDomain:  # noqa: N802
        return _AbsentDomain()


@pytest.mark.parametrize(
    ("kind", "survives"), [(HotplugDeviceKind.NIC, True), (HotplugDeviceKind.DISK, False)]
)
def test_absent_device_unlinks_a_file_only_for_a_disk(
    tmp_path: Path, kind: HotplugDeviceKind, survives: bool
) -> None:
    device_id = str(uuid4())
    volumes = LocalDeviceVolumes(str(tmp_path))
    leftover = tmp_path / f"{scratch_disk_prefix(DOMAIN)}{device_id}.qcow2"
    leftover.write_bytes(b"qcow2")

    @contextmanager
    def connection() -> Generator[_Conn]:
        yield _Conn()

    port = LibvirtDeviceHotplug(
        connection=connection,
        volumes=volumes,
        nic_source=hotplug._nic_source,  # noqa: SLF001
        kinds=hotplug.ADVERTISED_KINDS,
    )

    assert port.unplug(DOMAIN, device_id, None, kind=kind) is True
    assert leftover.exists() is survives
    leftover.write_bytes(b"qcow2")
    assert port.is_attached(DOMAIN, device_id, None, kind=kind) is False
    assert leftover.exists() is survives
