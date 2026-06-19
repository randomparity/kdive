"""Inventory-backed config for the remote-libvirt provider (ADR-0076, ADR-0077, ADR-0112).

Phase 3 (#395) deletes the ``KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR}`` singletons; the
remote connection config is now resolved per op from the ``systems.toml`` ``[[remote_libvirt]]``
instance. The libvirt storage-pool / network / machine knobs stay operational env settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import kdive.config as config
import kdive.providers.remote_libvirt.config as config_module
from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.model import RemoteLibvirtInstance
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    TlsCertRefs,
    all_remote_configs,
    is_remote_libvirt_configured,
    remote_config_for_resource,
    remote_config_from_inventory,
    resolve_base_image_staged_volume,
)
from kdive.providers.remote_libvirt.lifecycle.gdb import allocate_gdb_port

_INSTANCE = """
name = "ub24-big"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "remote/clientcert.pem"
client_key_ref = "remote/clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote/cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
vcpus = 16
memory_mb = 65536
"""

_IMAGE = """
[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"
"""


def _write_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    instances: str = _INSTANCE,
    image: str = _IMAGE,
) -> Path:
    blocks = "".join(f"[[remote_libvirt]]{block}" for block in instances.split("---") if block)
    doc = f"schema_version = 2\n{image}\n{blocks}\n"
    path = tmp_path / "systems.toml"
    path.write_text(doc)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    return path


def _no_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    config.load()


def test_single_instance_builds_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.uri == "qemu+tls://host.example/system"
    assert cfg.cert_refs.client_cert_ref == "remote/clientcert.pem"
    assert cfg.cert_refs.client_key_ref == "remote/clientkey.pem"  # pragma: allowlist secret
    assert cfg.cert_refs.ca_cert_ref == "remote/cacert.pem"
    assert cfg.gdb_addr == "192.168.10.20"
    assert cfg.gdb_port_min == 47000
    assert cfg.gdb_port_max == 47099
    assert cfg.concurrent_allocation_cap == 1  # model default


def test_configured_detection_tracks_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    assert not is_remote_libvirt_configured()
    _write_inventory(tmp_path, monkeypatch)
    assert is_remote_libvirt_configured()


def test_gate_degrades_on_malformed_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The opt-in gate runs at app startup and must not crash the server on a bad operator edit:
    # a malformed systems.toml reads as "not configured" (the op-time resolver still fails closed).
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n[[remote_libvirt]\n")  # malformed table header
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    assert is_remote_libvirt_configured() is False


def test_no_instance_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_multiple_instances_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    second = _INSTANCE.replace('name = "ub24-big"', 'name = "ub24-small"')
    _write_inventory(tmp_path, monkeypatch, instances=f"{_INSTANCE}---{second}")
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "multiple" in str(excinfo.value)


def test_malformed_inventory_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n[[remote_libvirt]\n")  # malformed table header
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def _instance(name: str, uri: str) -> RemoteLibvirtInstance:
    return RemoteLibvirtInstance(
        name=name,
        uri=uri,
        gdb_addr="192.168.10.20",
        gdbstub_range="47000:47099",
        client_cert_ref="remote/clientcert.pem",
        client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
        ca_cert_ref="remote/cacert.pem",
        base_image="base",
        cost_class="remote",
        vcpus=16,
        memory_mb=65536,
    )


def _two_instances() -> list[RemoteLibvirtInstance]:
    return [
        _instance("host-a", "qemu+tls://a.example/system"),
        _instance("host-b", "qemu+tls://b.example/system"),
    ]


def test_remote_config_for_resource_selects_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_load_remote_instances", _two_instances)
    assert remote_config_for_resource("host-b").uri == "qemu+tls://b.example/system"
    assert remote_config_for_resource("host-a").uri == "qemu+tls://a.example/system"


def test_remote_config_for_resource_unknown_name_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "_load_remote_instances", _two_instances)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource("nope")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "nope" in str(excinfo.value)


def test_all_remote_configs_returns_every_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_load_remote_instances", _two_instances)
    uris = sorted(cfg.uri for cfg in all_remote_configs())
    assert uris == ["qemu+tls://a.example/system", "qemu+tls://b.example/system"]


def test_remote_config_for_resource_validates_selected_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _two_instances()
    bad[1] = bad[1].model_copy(update={"uri": "qemu+tls://b.example/system?no_verify=1"})
    monkeypatch.setattr(config_module, "_load_remote_instances", lambda: bad)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource("host-b")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_uri_with_no_verify_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _INSTANCE.replace(
        "qemu+tls://host.example/system", "qemu+tls://host.example/system?no_verify=1"
    )
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_explicit_cap_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = f"{_INSTANCE}concurrent_allocation_cap = 4\n"
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    assert remote_config_from_inventory().concurrent_allocation_cap == 4


def test_provisioning_knob_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.storage_pool == "default"
    assert cfg.network == "default"
    assert cfg.machine == "pc"


def test_provisioning_knobs_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_STORAGE_POOL", "kdive-pool")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_NETWORK", "lab-net")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_MACHINE", "q35")
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.storage_pool == "kdive-pool"
    assert cfg.network == "lab-net"
    assert cfg.machine == "q35"


def test_resolve_base_image_staged_volume_returns_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inventory(tmp_path, monkeypatch)
    assert resolve_base_image_staged_volume() == "fedora-kdive-remote-base-43.qcow2"


def test_resolve_base_image_staged_volume_no_instance_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_base_image_staged_volume_multiple_instances_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = _INSTANCE.replace('name = "ub24-big"', 'name = "ub24-small"')
    _write_inventory(tmp_path, monkeypatch, instances=f"{_INSTANCE}---{second}")
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "multiple" in str(excinfo.value)


def test_resolve_base_image_staged_volume_absent_image_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The instance's base_image names an image not in the [[image]] list. The inventory loader
    # validates this cross-ref, so a present-but-mismatched doc cannot normally load; force the
    # drift by writing the doc directly (loader rejects it -> configuration_error at parse time).
    instance = _INSTANCE.replace(
        'base_image = "fedora-kdive-remote-base-43"', 'base_image = "no-such-image"'
    )
    path = tmp_path / "systems.toml"
    block = "".join(f"[[remote_libvirt]]{instance}")
    path.write_text(f"schema_version = 2\n{_IMAGE}\n{block}\n")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_base_image_staged_volume_non_staged_image_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3_image = """
[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "s3"
object_key = "images/fedora.qcow2"
"""
    _write_inventory(tmp_path, monkeypatch, image=s3_image)
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "staged" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["low:47099", "47000", "0:47099", "47099:47000", "47000:47000"])
def test_bad_gdbstub_range_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', f'gdbstub_range = "{bad}"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_single_port_range_names_the_reserved_probe_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A one-port range leaves nothing assignable once the lowest port is reserved for the ACL
    # probe; the error must say so actionably (ADR-0184).
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47000:47000"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_inventory()
    message = str(excinfo.value)
    assert "at least 2 ports" in message
    assert "reserved for the ACL probe" in message


def test_two_port_range_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: exactly one reserved probe port + one assignable System port.
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47000:47001"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    cfg = remote_config_from_inventory()
    assert (cfg.gdb_port_min, cfg.gdb_port_max) == (47000, 47001)


def test_acl_probe_port_and_assignable_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lowest port is the reserved ACL probe port; System allocation starts one above it
    # (ADR-0184).
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_from_inventory()
    assert cfg.acl_probe_port == cfg.gdb_port_min == 47000
    assert cfg.assignable_gdb_port_min == cfg.gdb_port_min + 1 == 47001
    assert cfg.assignable_gdb_port_min <= cfg.gdb_port_max


def test_probe_target_is_the_reserved_never_allocated_port() -> None:
    # End-to-end invariant (ADR-0184): the port the gdbstub_acl probe actually connects to is
    # exactly the reserved acl_probe_port, AND the System allocator (driven from the assignable
    # floor) never hands that port out. Pins all three facts to one config so a later divergence
    # — a changed range-string format, a probe that targeted a different port, or an allocator
    # passed the wrong floor — fails here, not silently in production.
    cfg = RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
        concurrent_allocation_cap=1,
        gdb_addr="10.0.0.5",
        gdb_port_min=47000,
        gdb_port_max=47099,
    )
    # The contribution formats the probe's range exactly this way (contribution.py).
    port_range = f"{cfg.gdb_port_min}-{cfg.gdb_port_max}"

    captured: dict[str, int] = {}

    def fake_connector(host: str, port: int) -> None:
        captured["port"] = port

    probe = gdbstub_acl_probe(connector=fake_connector)
    asyncio.run(probe(cfg.gdb_addr or "", port_range))

    assert captured["port"] == cfg.acl_probe_port
    allocated = allocate_gdb_port(
        {},
        own_name="kdive-x",
        port_min=cfg.assignable_gdb_port_min,
        port_max=cfg.gdb_port_max,
    )
    assert allocated != cfg.acl_probe_port
