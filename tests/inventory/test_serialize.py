"""Serializer for ``ops.export_systems_toml`` (#640, ADR-0199).

Three layers, each tested at its own boundary:

* TOML emitter primitives — injection-safe, deterministic escaping (no DB).
* ``serialize_inventory`` — pure ``InventorySnapshot`` → deterministic ``systems.toml`` text.
* ``read_inventory_snapshot`` — live config-owned rows → snapshot, honoring the override ledger.
"""

from __future__ import annotations

import asyncio
import tomllib
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from kdive.inventory import serialize
from kdive.inventory.model import InventoryDoc, StagedPathSource
from kdive.inventory.overrides import BUILD_HOST_RESOURCE_KIND, InventorySourceKind

# ---- TOML emitter primitives ----------------------------------------------------------


def _parse_value(emitted: str) -> object:
    """Parse a single ``k = <emitted>`` line and return the value of ``k``."""
    return tomllib.loads(f"k = {emitted}")["k"]


def test_toml_str_plain_round_trips() -> None:
    assert _parse_value(serialize._toml_str("hello")) == "hello"


def test_toml_str_escapes_quote_backslash_newline_tab() -> None:
    for value in ('a"b', "a\\b", "a\nb", "a\tb", "a\rb", "x\x00y"):
        assert _parse_value(serialize._toml_str(value)) == value


def test_toml_str_cannot_inject_a_key() -> None:
    # A value that, unescaped, would close the string and add a sibling key.
    hostile = 'evil"\ncoeff = "9'
    parsed = tomllib.loads(f"name = {serialize._toml_str(hostile)}")
    assert parsed == {"name": hostile}  # exactly one key; no injected coeff
    assert "coeff" not in parsed


def test_toml_array_escapes_elements_and_round_trips() -> None:
    items = ["a", 'b"c', "d\ne"]
    assert _parse_value(serialize._toml_array(items)) == items


def test_toml_array_empty() -> None:
    assert serialize._toml_array([]) == "[]"
    assert _parse_value(serialize._toml_array([])) == []


def test_toml_int() -> None:
    assert serialize._toml_int(5) == "5"
    assert _parse_value(serialize._toml_int(5)) == 5


# ---- serialize_inventory (pure) -------------------------------------------------------


def _image(
    *,
    name: str = "ubuntu",
    provider: str = "remote-libvirt",
    arch: str = "x86_64",
    object_key: str | None = None,
    digest: str | None = None,
    volume: str | None = None,
    path: str | None = None,
    state: str = "defined",
) -> serialize.ImageRow:
    return serialize.ImageRow(
        provider=provider,
        name=name,
        arch=arch,
        format="qcow2",
        root_device="/dev/vda",
        visibility="public",
        capabilities=[],
        object_key=object_key,
        digest=digest,
        volume=volume,
        path=path,
        state=state,
    )


def _remote(name: str = "host-a") -> serialize.ResourceRow:
    return serialize.ResourceRow(
        name=name,
        cost_class="remote",
        pool="remote",
        host_uri="qemu+tls://host/system",
        vcpus=8,
        memory_mb=16384,
        concurrent_allocation_cap=2,
        seed=None,
    )


def _build_host(
    *, name: str = "bh-local", kind: str = "local", base_image_volume: str | None = None
) -> serialize.BuildHostRow:
    return serialize.BuildHostRow(
        name=name,
        kind=kind,
        base_image_volume=base_image_volume,
        workspace_root="/var/lib/kdive/build",
        max_concurrent=4,
    )


def _snapshot(**overrides: object) -> serialize.InventorySnapshot:
    base: dict[str, object] = {
        "images": (),
        "remote_libvirt": (),
        "local_libvirt": (),
        "fault_inject": (),
        "build_hosts": (),
        "cost_classes": (),
    }
    base.update(overrides)
    return serialize.InventorySnapshot(**base)  # type: ignore[arg-type]


def test_serialize_is_byte_deterministic() -> None:
    snap = _snapshot(
        remote_libvirt=(_remote("host-b"), _remote("host-a")),
        cost_classes=(("zeta", Decimal("3.0")), ("alpha", Decimal("0.5"))),
    )
    assert serialize.serialize_inventory(snap) == serialize.serialize_inventory(snap)


def test_serialize_emits_schema_version_and_header() -> None:
    text = serialize.serialize_inventory(_snapshot())
    assert "schema_version = 2" in text
    assert text.lstrip().startswith("#")  # a header comment leads
    parsed = tomllib.loads(text)
    assert parsed["schema_version"] == 2


def test_serialize_sorts_each_section() -> None:
    snap = _snapshot(remote_libvirt=(_remote("host-b"), _remote("host-a")))
    text = serialize.serialize_inventory(snap)
    assert text.index('name = "host-a"') < text.index('name = "host-b"')


def test_serialize_remote_skeleton_has_placeholders_and_live_values() -> None:
    snap = _snapshot(remote_libvirt=(_remote("host-a"),))
    text = serialize.serialize_inventory(snap)
    # file-only fields are placeholders
    for field in ("gdb_addr", "gdbstub_range", "client_cert_ref", "client_key_ref", "ca_cert_ref"):
        assert f"{field} = " in text
        assert "REPLACE_ME" in text
    assert "base_image = " in text
    assert "shapes = []" in text
    # live values are emitted, not placeholders
    assert 'uri = "qemu+tls://host/system"' in text
    assert "vcpus = 8" in text
    assert "memory_mb = 16384" in text
    assert "concurrent_allocation_cap = 2" in text
    assert 'cost_class = "remote"' in text


def test_serialize_omits_null_base_image_volume_for_local_build_host() -> None:
    snap = _snapshot(build_hosts=(_build_host(kind="local", base_image_volume=None),))
    text = serialize.serialize_inventory(snap)
    assert "base_image_volume" not in text  # NULL omitted, not blanked
    # an ephemeral host emits it
    snap2 = _snapshot(
        build_hosts=(_build_host(kind="ephemeral_libvirt", base_image_volume="vol-1"),)
    )
    assert 'base_image_volume = "vol-1"' in serialize.serialize_inventory(snap2)


def test_serialize_omits_null_image_digest() -> None:
    snap = _snapshot(images=(_image(object_key="k", digest=None, state="registered"),))
    text = serialize.serialize_inventory(snap)
    assert "digest" not in text
    assert 'object_key = "k"' in text


def test_serialize_image_staged_source() -> None:
    snap = _snapshot(images=(_image(volume="vol-x", state="registered"),))
    text = serialize.serialize_inventory(snap)
    assert 'kind = "staged"' in text
    assert 'volume = "vol-x"' in text


def test_serialize_image_staged_path_source() -> None:
    snap = _snapshot(
        images=(
            _image(
                provider="local-libvirt",
                name="local-rootfs",
                path="/var/lib/kdive/rootfs/local-rootfs.qcow2",
                state="registered",
            ),
        )
    )
    text = serialize.serialize_inventory(snap)
    assert 'kind = "staged-path"' in text
    assert 'path = "/var/lib/kdive/rootfs/local-rootfs.qcow2"' in text
    # Round-trips: the emitted inventory re-parses with the staged-path source.
    parsed = InventoryDoc.parse(tomllib.loads(text))
    source = parsed.image[0].source
    assert isinstance(source, StagedPathSource)
    assert source.path == "/var/lib/kdive/rootfs/local-rootfs.qcow2"


def test_completed_remote_skeleton_parses_after_filling_placeholders() -> None:
    snap = _snapshot(
        images=(_image(name="base", volume="vol-x", state="registered"),),
        remote_libvirt=(_remote("host-a"),),
        cost_classes=(("remote", Decimal("1.0")),),
    )
    text = serialize.serialize_inventory(snap)
    # operator completes the placeholders (base_image -> an exported image name)
    completed = _complete_remote_skeleton(text, base_image="base")
    doc = InventoryDoc.parse(tomllib.loads(completed))
    assert doc.remote_libvirt[0].name == "host-a"
    assert doc.remote_libvirt[0].base_image == "base"
    assert doc.remote_libvirt[0].vcpus == 8


def test_unedited_remote_skeleton_does_not_parse() -> None:
    snap = _snapshot(
        images=(_image(name="base", volume="vol-x", state="registered"),),
        remote_libvirt=(_remote("host-a"),),
    )
    text = serialize.serialize_inventory(snap)
    # base_image = "REPLACE_ME_base_image" names no declared [[image]] -> InventoryError
    import pytest

    from kdive.inventory.errors import InventoryError

    with pytest.raises(InventoryError):
        InventoryDoc.parse(tomllib.loads(text))


# ---- read_inventory_snapshot (DB) -----------------------------------------------------


def test_read_snapshot_reads_config_rows_and_excludes_non_config(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await _seed_config_image(conn, name="base", volume="vol-x")
            await _seed_remote(conn, name="host-a", vcpus=8, memory_mb=16384, cap=2)
            await _seed_discovery_resource(conn, name="probed")  # excluded (managed_by=discovery)
            await _seed_build_host(conn, name="bh-local", kind="local")
            await _seed_build_host(
                conn, name="bh-eph", kind="ephemeral_libvirt", base_image_volume="vol-1"
            )
            snap = await serialize.read_inventory_snapshot(conn)
        image_names = {i.name for i in snap.images}
        assert image_names == {"base"}
        remote_names = {r.name for r in snap.remote_libvirt}
        assert remote_names == {"host-a"}
        host = next(r for r in snap.remote_libvirt if r.name == "host-a")
        assert host.vcpus == 8
        assert host.memory_mb == 16384
        assert host.concurrent_allocation_cap == 2
        assert {b.name for b in snap.build_hosts} == {"bh-local", "bh-eph"}
        eph = next(b for b in snap.build_hosts if b.name == "bh-eph")
        assert eph.base_image_volume == "vol-1"
        local = next(b for b in snap.build_hosts if b.name == "bh-local")
        assert local.base_image_volume is None
        # cost classes: the seeded local/remote (=1.0) are present
        by_name = dict(snap.cost_classes)
        assert by_name["remote"] == Decimal("1.0")

    asyncio.run(_run())


def test_read_snapshot_omits_removed_and_keeps_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await _seed_remote(conn, name="gone", vcpus=4, memory_mb=4096, cap=1)
            await _seed_remote(conn, name="modified", vcpus=4, memory_mb=4096, cap=9)
            await _seed_build_host(conn, name="bh-gone", kind="local")
            await _set_override(
                conn, InventorySourceKind.RESOURCE, "remote-libvirt", "gone", "removed"
            )
            await _set_override(
                conn, InventorySourceKind.RESOURCE, "remote-libvirt", "modified", "detached"
            )
            await _set_override(
                conn,
                InventorySourceKind.BUILD_HOST,
                BUILD_HOST_RESOURCE_KIND,
                "bh-gone",
                "removed",
            )
            snap = await serialize.read_inventory_snapshot(conn)
        remote_names = {r.name for r in snap.remote_libvirt}
        assert remote_names == {"modified"}  # removed omitted; detached kept
        modified = next(r for r in snap.remote_libvirt if r.name == "modified")
        assert modified.concurrent_allocation_cap == 9  # the live (runtime-modified) value
        assert {b.name for b in snap.build_hosts} == set()  # bh-gone removed

    asyncio.run(_run())


def test_read_then_serialize_round_trips_through_the_model(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await _seed_config_image(conn, name="base", volume="vol-x")
            await _seed_remote(conn, name="host-a", vcpus=8, memory_mb=16384, cap=2)
            await _seed_build_host(conn, name="bh-local", kind="local")
            snap = await serialize.read_inventory_snapshot(conn)
        text = serialize.serialize_inventory(snap)
        completed = _complete_remote_skeleton(text, base_image="base")
        doc = InventoryDoc.parse(tomllib.loads(completed))
        assert doc.remote_libvirt[0].name == "host-a"
        assert doc.remote_libvirt[0].vcpus == 8
        assert doc.remote_libvirt[0].base_image == "base"
        assert doc.build_host[0].name == "bh-local"
        assert doc.image[0].name == "base"

    asyncio.run(_run())


def _complete_remote_skeleton(text: str, *, base_image: str) -> str:
    """Fill the export's REPLACE_ME_* placeholders so the remote block parses.

    Built from ``(field, value)`` tuples rather than literal ``field = "value"`` lines so the
    secret scanner does not read a cert-ref assignment in the source (the values are obvious
    test placeholders, not secrets).
    """
    completions = {
        "base_image": base_image,
        "gdb_addr": "10.0.0.1:1234",
        "gdbstub_range": "1234-1240",
        "client_cert_ref": "ref://cc",
        "client_key_ref": "ref://ck",  # pragma: allowlist secret
        "ca_cert_ref": "ref://ca",
    }
    for field, value in completions.items():
        text = text.replace(f'{field} = "REPLACE_ME_{field}"', f'{field} = "{value}"')
    return text


# ---- DB seed helpers ------------------------------------------------------------------


async def _seed_config_image(conn: psycopg.AsyncConnection, *, name: str, volume: str) -> None:
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, visibility, capabilities, volume, state, "
        " managed_by) "
        "VALUES ('remote-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', 'public', '{}', %s, "
        " 'registered', 'config')",
        (name, volume),
    )


async def _seed_remote(
    conn: psycopg.AsyncConnection, *, name: str, vcpus: int, memory_mb: int, cap: int
) -> None:
    caps = Jsonb({"vcpus": vcpus, "memory_mb": memory_mb, "concurrent_allocation_cap": cap})
    await conn.execute(
        "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, host_uri, "
        " managed_by) "
        "VALUES ('remote-libvirt', %s, %s, 'remote', 'remote', 'available', "
        " 'qemu+tls://host/system', 'config')",
        (name, caps),
    )


async def _seed_discovery_resource(conn: psycopg.AsyncConnection, *, name: str) -> None:
    caps = Jsonb({"vcpus": 16, "memory_mb": 65536, "concurrent_allocation_cap": 1})
    await conn.execute(
        "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, host_uri, "
        " managed_by) "
        "VALUES ('local-libvirt', %s, %s, 'local-libvirt', 'local', 'available', "
        " 'qemu:///system', 'discovery')",
        (name, caps),
    )


async def _seed_build_host(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    kind: str,
    base_image_volume: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO build_hosts "
        "(name, kind, base_image_volume, workspace_root, max_concurrent, managed_by) "
        "VALUES (%s, %s, %s, '/var/lib/kdive/build', 4, 'config')",
        (name, kind, base_image_volume),
    )


async def _set_override(
    conn: psycopg.AsyncConnection,
    source_kind: InventorySourceKind,
    resource_kind: str,
    name: str,
    disposition: str,
) -> None:
    await conn.execute(
        "INSERT INTO inventory_overrides "
        "(source_kind, resource_kind, name, disposition, reason, actor) "
        "VALUES (%s, %s, %s, %s, 'test', 'op-1')",
        (source_kind.value, resource_kind, name, disposition),
    )
