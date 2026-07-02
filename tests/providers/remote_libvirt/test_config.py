"""Inventory-backed config for the remote-libvirt provider (ADR-0076, ADR-0077, ADR-0112).

Phase 3 (#395) deletes the ``KDIVE_REMOTE_LIBVIRT_{URI,*_CERT_REF,GDB_ADDR}`` singletons; the
remote connection config is now resolved per op from the ``systems.toml`` ``[[remote_libvirt]]``
instance. The libvirt storage-pool / network / machine knobs stay operational env settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import libvirt
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
    resolve_base_image_staged_volume_for,
)
from kdive.providers.remote_libvirt.lifecycle.gdb import (
    DOMAIN_PREFIX,
    Domain,
    allocate_gdb_port,
    used_gdb_ports,
)

_RESOURCE = "ub24-big"

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


def test_singleton_resolver_is_gone() -> None:
    # ADR-0187, #395: the no-arg singleton resolver and its guards are deleted; per-op callers
    # resolve by resource name. Guard the deletion so a stray re-introduction is caught.
    assert not hasattr(config_module, "remote_config_from_inventory")
    assert not hasattr(config_module, "_require_single_instance")
    assert not hasattr(config_module, "_resolve_instance")
    assert not hasattr(config_module, "resolve_base_image_staged_volume")


def test_by_name_builds_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_for_resource(_RESOURCE)
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


def test_unknown_resource_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_multiple_instances_each_resolvable_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0187, #395: multiple instances are now supported; each resolves by its own name.
    second = _INSTANCE.replace('name = "ub24-big"', 'name = "ub24-small"').replace(
        "qemu+tls://host.example/system", "qemu+tls://host2.example/system"
    )
    _write_inventory(tmp_path, monkeypatch, instances=f"{_INSTANCE}---{second}")
    assert remote_config_for_resource("ub24-big").uri == "qemu+tls://host.example/system"
    assert remote_config_for_resource("ub24-small").uri == "qemu+tls://host2.example/system"


def test_malformed_inventory_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text("schema_version = 2\n[[remote_libvirt]\n")  # malformed table header
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value).startswith("systems.toml is present but invalid:")


def _instance(
    name: str,
    uri: str,
    *,
    gdb_addr: str = "192.168.10.20",
    ssh_addr: str | None = None,
    ssh_range: str | None = None,
) -> RemoteLibvirtInstance:
    return RemoteLibvirtInstance(
        name=name,
        uri=uri,
        gdb_addr=gdb_addr,
        gdbstub_range="47000:47099",
        ssh_addr=ssh_addr,
        ssh_range=ssh_range,
        client_cert_ref="remote/clientcert.pem",
        client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
        ca_cert_ref="remote/cacert.pem",
        base_image="base",
        cost_class="remote",
        vcpus=16,
        memory_mb=65536,
    )


def test_ssh_parity_inactive_when_unset() -> None:
    cfg = config_module._build_config(_instance("rl", "qemu+tls://h/system"))
    assert cfg.ssh_parity_active is False
    assert cfg.ssh_addr is None
    assert (cfg.ssh_port_min, cfg.ssh_port_max) == (None, None)


def test_ssh_parity_active_parses_range() -> None:
    cfg = config_module._build_config(
        _instance("rl", "qemu+tls://h/system", ssh_addr="10.0.0.9", ssh_range="47100:47199")
    )
    assert cfg.ssh_parity_active is True
    assert cfg.ssh_addr == "10.0.0.9"
    assert (cfg.ssh_port_min, cfg.ssh_port_max) == (47100, 47199)


def test_ssh_range_single_port_is_valid() -> None:
    # Unlike gdbstub (which reserves the lowest for the ACL probe), a one-port SSH range is fine.
    cfg = config_module._build_config(
        _instance("rl", "qemu+tls://h/system", ssh_addr="10.0.0.9", ssh_range="47100:47100")
    )
    assert (cfg.ssh_port_min, cfg.ssh_port_max) == (47100, 47100)


def test_half_configured_ssh_addr_only_is_error() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        config_module._build_config(_instance("rl", "qemu+tls://h/system", ssh_addr="10.0.0.9"))
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_half_configured_ssh_range_only_is_error() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        config_module._build_config(_instance("rl", "qemu+tls://h/system", ssh_range="47100:47199"))
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_ssh_range_inverted_is_error() -> None:
    with pytest.raises(CategorizedError):
        config_module._build_config(
            _instance("rl", "qemu+tls://h/system", ssh_addr="10.0.0.9", ssh_range="500:400")
        )


def test_ssh_range_overlap_on_shared_gdb_addr_is_error() -> None:
    # ssh_addr == gdb_addr and the ranges overlap → they would contend for one host socket.
    with pytest.raises(CategorizedError) as excinfo:
        config_module._build_config(
            _instance(
                "rl",
                "qemu+tls://h/system",
                gdb_addr="10.0.0.1",
                ssh_addr="10.0.0.1",
                ssh_range="47050:47150",
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_ssh_range_overlap_allowed_on_distinct_addr() -> None:
    cfg = config_module._build_config(
        _instance(
            "rl",
            "qemu+tls://h/system",
            gdb_addr="10.0.0.1",
            ssh_addr="10.0.0.2",
            ssh_range="47050:47150",
        )
    )
    assert cfg.ssh_parity_active is True


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
    message = str(excinfo.value)
    assert "nope" in message
    # The error lists the sorted declared instance names so an operator sees the valid set.
    assert "(declared: ['host-a', 'host-b'])" in message


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
        remote_config_for_resource(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_explicit_cap_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = f"{_INSTANCE}concurrent_allocation_cap = 4\n"
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    assert remote_config_for_resource(_RESOURCE).concurrent_allocation_cap == 4


def test_provisioning_knob_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_for_resource(_RESOURCE)
    assert cfg.storage_pool == "default"
    assert cfg.network == "default"
    assert cfg.machine == "pc"


def test_provisioning_knobs_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_STORAGE_POOL", "kdive-pool")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_NETWORK", "lab-net")
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_MACHINE", "q35")
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_for_resource(_RESOURCE)
    assert cfg.storage_pool == "kdive-pool"
    assert cfg.network == "lab-net"
    assert cfg.machine == "q35"


def test_resolve_base_image_staged_volume_returns_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inventory(tmp_path, monkeypatch)
    assert resolve_base_image_staged_volume_for(_RESOURCE) == "fedora-kdive-remote-base-43.qcow2"


def test_resolve_base_image_staged_volume_unknown_resource_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume_for(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_base_image_unknown_instance_lists_declared_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inventory(tmp_path, monkeypatch)
    with pytest.raises(CategorizedError) as excinfo:
        resolve_base_image_staged_volume_for("no-such-host")
    message = str(excinfo.value)
    assert "no-such-host" in message
    assert "(declared: ['ub24-big'])" in message


def test_resolve_base_image_staged_volume_each_instance_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0187, #395: with multiple instances, the by-name resolver returns each host's own
    # staged volume.
    second = _INSTANCE.replace('name = "ub24-big"', 'name = "ub24-small"').replace(
        "qemu+tls://host.example/system", "qemu+tls://host2.example/system"
    )
    _write_inventory(tmp_path, monkeypatch, instances=f"{_INSTANCE}---{second}")
    assert resolve_base_image_staged_volume_for("ub24-small") == "fedora-kdive-remote-base-43.qcow2"


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
        resolve_base_image_staged_volume_for(_RESOURCE)
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
        resolve_base_image_staged_volume_for(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "staged" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["low:47099", "47000", "0:47099", "47099:47000", "47000:47000"])
def test_bad_gdbstub_range_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', f'gdbstub_range = "{bad}"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_staged_volume_absent_image_raises_categorized_not_stopiteration() -> None:
    # Directly exercise the absent-image branch: with no matching [[image]], the lookup must
    # surface a CONFIGURATION_ERROR, not leak a bare StopIteration from next().
    instance = _instance("host-a", "qemu+tls://a.example/system")
    with pytest.raises(CategorizedError) as excinfo:
        config_module._staged_volume_for_instance(instance, [])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "names no" in str(excinfo.value)


@pytest.mark.parametrize(
    ("rng", "expected"),
    [
        ("1:65535", (1, 65535)),  # extreme-but-valid bounds (ports 1 and 65535 are in range)
        ("47000:47001", (47000, 47001)),
    ],
)
def test_valid_gdbstub_range_bounds_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rng: str,
    expected: tuple[int, int],
) -> None:
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', f'gdbstub_range = "{rng}"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    cfg = remote_config_for_resource(_RESOURCE)
    assert (cfg.gdb_port_min, cfg.gdb_port_max) == expected


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        ("47000:65536", "outside 1..65535"),  # upper bound: 65536 is rejected
        ("notint:47099", "non-integer ports"),
        ("47000", "is not 'min:max'"),
        ("47099:47000", "is inverted"),
    ],
)
def test_bad_gdbstub_range_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
    needle: str,
) -> None:
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', f'gdbstub_range = "{bad}"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource(_RESOURCE)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert needle in str(excinfo.value)


def test_single_port_range_names_the_reserved_probe_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A one-port range leaves nothing assignable once the lowest port is reserved for the ACL
    # probe; the error must say so actionably (ADR-0184).
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47000:47000"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_for_resource(_RESOURCE)
    message = str(excinfo.value)
    assert "at least 2 ports" in message
    assert "reserved for the ACL probe" in message


def test_two_port_range_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: exactly one reserved probe port + one assignable System port.
    instance = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47000:47001"')
    _write_inventory(tmp_path, monkeypatch, instances=instance)
    cfg = remote_config_for_resource(_RESOURCE)
    assert (cfg.gdb_port_min, cfg.gdb_port_max) == (47000, 47001)


def test_acl_probe_port_and_assignable_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lowest port is the reserved ACL probe port; System allocation starts one above it
    # (ADR-0184).
    _write_inventory(tmp_path, monkeypatch)
    cfg = remote_config_for_resource(_RESOURCE)
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


def _gdb_domain_xml(port: int) -> str:
    return (
        "<domain><qemu:commandline "
        'xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">'
        '<qemu:arg value="-gdb"/>'
        f'<qemu:arg value="tcp:10.0.0.5:{port}"/>'
        "</qemu:commandline></domain>"
    )


class _FakeDomain:
    def __init__(self, name: str, port: int) -> None:
        self._name = name
        self._port = port

    def name(self) -> str:
        return self._name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt API name
        return _gdb_domain_xml(self._port)


class _FakeConn:
    def __init__(self, domains: list[Domain]) -> None:
        self._domains = domains

    def listAllDomains(self, flags: int = 0):  # noqa: N802 - libvirt API name
        return self._domains


def test_allocate_gdb_port_reuses_own_recorded_in_range_port() -> None:
    # A System's own recorded in-range port is returned (stable across retries), not a fresh one.
    assert (
        allocate_gdb_port({"kdive-x": 47005}, own_name="kdive-x", port_min=47001, port_max=47099)
        == 47005
    )


def test_allocate_gdb_port_reuses_own_port_at_min_boundary() -> None:
    # own == port_min is inclusive: it is reused even though another domain holds the next
    # port (so a fall-through to lowest-free would return a different port, exposing a
    # `<=` -> `<` boundary mutation).
    used = {"kdive-x": 47001, "kdive-y": 47002}
    assert allocate_gdb_port(used, own_name="kdive-x", port_min=47001, port_max=47099) == 47001


def test_allocate_gdb_port_reuses_own_port_at_max_boundary() -> None:
    # own == port_max is inclusive: it is reused even though every lower port is taken (so a
    # fall-through would otherwise raise exhaustion or pick a lower port).
    used = {"kdive-x": 47002, "kdive-y": 47001}
    assert allocate_gdb_port(used, own_name="kdive-x", port_min=47001, port_max=47002) == 47002


def test_allocate_gdb_port_picks_lowest_free_when_no_own_port() -> None:
    # 47001 is taken by another domain, so the lowest free port is 47002.
    assert (
        allocate_gdb_port({"kdive-y": 47001}, own_name="kdive-x", port_min=47001, port_max=47099)
        == 47002
    )


def test_allocate_gdb_port_exhausted_range_is_provisioning_failure() -> None:
    used = {"kdive-y": 47001, "kdive-z": 47002}
    with pytest.raises(CategorizedError) as excinfo:
        allocate_gdb_port(used, own_name="kdive-x", port_min=47001, port_max=47002)
    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(excinfo.value) == "gdbstub port range is exhausted on the remote host"
    assert excinfo.value.details == {"port_min": 47001, "port_max": 47002, "in_use": 2}


def test_used_gdb_ports_maps_kdive_domains_to_recorded_ports() -> None:
    conn = _FakeConn(
        [
            _FakeDomain(f"{DOMAIN_PREFIX}a", 47010),
            _FakeDomain(f"{DOMAIN_PREFIX}b", 47011),
            _FakeDomain("other-vm", 47012),  # non-kdive domains are skipped
        ]
    )
    assert used_gdb_ports(conn) == {f"{DOMAIN_PREFIX}a": 47010, f"{DOMAIN_PREFIX}b": 47011}


def test_used_gdb_ports_continues_past_a_skipped_domain() -> None:
    # The non-kdive domain in the middle is skipped via `continue`, not a `break` that would
    # drop the trailing kdive domain.
    conn = _FakeConn(
        [
            _FakeDomain(f"{DOMAIN_PREFIX}a", 47010),
            _FakeDomain("other-vm", 47012),
            _FakeDomain(f"{DOMAIN_PREFIX}b", 47011),
        ]
    )
    assert used_gdb_ports(conn) == {f"{DOMAIN_PREFIX}a": 47010, f"{DOMAIN_PREFIX}b": 47011}


def test_used_gdb_ports_listing_failure_is_infrastructure_failure() -> None:
    class _BoomConn:
        def listAllDomains(self, flags: int = 0):  # noqa: N802 - libvirt API name
            raise libvirt.libvirtError("boom")

    with pytest.raises(CategorizedError) as excinfo:
        used_gdb_ports(_BoomConn())
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "libvirt error listing domains for gdbstub port enumeration"
    assert excinfo.value.details == {}


class _ErrDomain:
    def __init__(self, name: str, code: int) -> None:
        self._name = name
        self._code = code

    def name(self) -> str:
        return self._name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt API name
        exc = libvirt.libvirtError("boom")
        exc.get_error_code = lambda: self._code  # ty: ignore[invalid-assignment]
        raise exc


def test_used_gdb_ports_skips_a_domain_that_vanishes_mid_walk() -> None:
    # A domain disappearing mid-walk (VIR_ERR_NO_DOMAIN) is skipped, not fatal; the surviving
    # kdive domain is still enumerated.
    conn = _FakeConn(
        [
            _ErrDomain(f"{DOMAIN_PREFIX}gone", libvirt.VIR_ERR_NO_DOMAIN),
            _FakeDomain(f"{DOMAIN_PREFIX}b", 47011),
        ]
    )
    assert used_gdb_ports(conn) == {f"{DOMAIN_PREFIX}b": 47011}


def test_used_gdb_ports_per_domain_error_is_infrastructure_failure() -> None:
    # A non-NO_DOMAIN libvirt error reading a domain's XML is an infrastructure fault.
    conn = _FakeConn([_ErrDomain(f"{DOMAIN_PREFIX}a", libvirt.VIR_ERR_INTERNAL_ERROR)])
    with pytest.raises(CategorizedError) as excinfo:
        used_gdb_ports(conn)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "libvirt error enumerating gdbstub ports"
    assert excinfo.value.details == {}
