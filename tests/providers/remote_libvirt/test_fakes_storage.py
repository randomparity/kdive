"""Prove the storage double models libvirt's readback instead of echoing the request (#2164).

Every fact asserted here was read off real libvirt 12.0.0 against a ``dir`` pool over a
temporary directory; the design records the probe and its output in
``docs/workflow/specs/2026-09-02-libvirt-storage-double-fidelity-design.md``.

The load-bearing assertion is `test_readback_is_not_the_submitted_document`. A double that
returns its input passes a surprising number of weaker checks, so several tests here exist
specifically to fail when `XMLDesc` is reverted to an echo.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import libvirt
import pytest

from kdive.providers.remote_libvirt.lifecycle.xml import render_volume_xml
from tests.providers.remote_libvirt.fakes import FakeStorageConn, FakeStoragePool

POOL_TARGET = "/pool/target"

MODELLED_TOP_LEVEL = ("name", "key", "capacity", "allocation", "physical", "target")
MODELLED_BRANCH = ("path", "format", "permissions", "timestamps")
MODELLED_PERMISSIONS = ("mode", "owner", "group", "label")
MODELLED_TIMESTAMPS = ("atime", "mtime", "ctime", "btime")


@pytest.fixture
def pool() -> FakeStoragePool:
    return FakeStoragePool(target_path=POOL_TARGET)


def volume_document(
    name: str = "disk.qcow2",
    *,
    capacity: str | None = "1048576",
    unit: str | None = "bytes",
    root_type: str | None = None,
    format_type: str | None = "qcow2",
    mode: str | None = None,
    backing_path: str | None = None,
    extra: str = "",
) -> str:
    """Build a volume document. Only what a caller names is emitted."""
    root_attr = f" type='{root_type}'" if root_type is not None else ""
    unit_attr = "" if unit is None else f" unit='{unit}'"
    parts = [f"<volume{root_attr}><name>{name}</name>"]
    if capacity is not None:
        parts.append(f"<capacity{unit_attr}>{capacity}</capacity>")
    target: list[str] = []
    if format_type is not None:
        target.append(f"<format type='{format_type}'/>")
    if mode is not None:
        target.append(f"<permissions><mode>{mode}</mode></permissions>")
    if target:
        parts.append(f"<target>{''.join(target)}</target>")
    if backing_path is not None:
        parts.append(
            f"<backingStore><path>{backing_path}</path><format type='qcow2'/></backingStore>"
        )
    parts.append(extra)
    parts.append("</volume>")
    return "".join(parts)


def find(element: ET.Element, path: str) -> ET.Element:
    """Locate a required element, failing the test rather than returning None."""
    found = element.find(path)
    assert found is not None, f"readback is missing {path!r}"
    return found


def text_at(element: ET.Element, path: str) -> str | None:
    return find(element, path).text


def child_tags(element: ET.Element) -> tuple[str, ...]:
    return tuple(child.tag for child in element)


def readback(pool: FakeStoragePool, document: str) -> ET.Element:
    return ET.fromstring(pool.createXML(document).XMLDesc(0))


# --- the discard itself -------------------------------------------------------------------


def test_readback_drops_submitted_metadata_element(pool: FakeStoragePool) -> None:
    document = volume_document(
        extra=(
            "<metadata><kdive:owner xmlns:kdive='https://kdive.invalid/ns'>"
            "run-1</kdive:owner></metadata>"
        )
    )
    desc = pool.createXML(document).XMLDesc(0)
    assert "metadata" not in desc
    assert "run-1" not in desc


def test_readback_drops_unknown_elements(pool: FakeStoragePool) -> None:
    desc = pool.createXML(volume_document(extra="<bogusElement>zzz</bogusElement>")).XMLDesc(0)
    assert "bogusElement" not in desc
    assert "zzz" not in desc


def test_readback_is_not_the_submitted_document(pool: FakeStoragePool) -> None:
    document = volume_document()
    assert pool.createXML(document).XMLDesc(0) != document


def test_readback_drops_attributes_libvirt_does_not_keep(pool: FakeStoragePool) -> None:
    document = (
        "<volume kdive='owned'><name kdive='owned'>disk.qcow2</name>"
        "<capacity unit='bytes'>4096</capacity></volume>"
    )
    assert "kdive" not in pool.createXML(document).XMLDesc(0)


# --- the modelled tag set -----------------------------------------------------------------


def test_readback_renders_the_modelled_top_level_tags(pool: FakeStoragePool) -> None:
    root = readback(pool, volume_document(backing_path=f"{POOL_TARGET}/base.qcow2"))
    assert child_tags(root) == (*MODELLED_TOP_LEVEL, "backingStore")


def test_readback_omits_backing_store_when_none_was_submitted(pool: FakeStoragePool) -> None:
    volume = pool.createXML(volume_document())
    assert child_tags(ET.fromstring(volume.XMLDesc(0))) == MODELLED_TOP_LEVEL
    assert "backingStore" not in volume.XMLDesc(0)


def test_readback_renders_the_modelled_target_tags(pool: FakeStoragePool) -> None:
    target = find(readback(pool, volume_document()), "target")
    assert child_tags(target) == MODELLED_BRANCH
    assert child_tags(find(target, "permissions")) == MODELLED_PERMISSIONS
    assert child_tags(find(target, "timestamps")) == MODELLED_TIMESTAMPS


def test_readback_renders_the_modelled_backing_store_tags(pool: FakeStoragePool) -> None:
    root = readback(pool, volume_document(backing_path=f"{POOL_TARGET}/base.qcow2"))
    backing = find(root, "backingStore")
    assert child_tags(backing) == MODELLED_BRANCH
    assert child_tags(find(backing, "permissions")) == MODELLED_PERMISSIONS
    assert child_tags(find(backing, "timestamps")) == MODELLED_TIMESTAMPS


# --- retained fields ----------------------------------------------------------------------


def test_readback_retains_submitted_fields(pool: FakeStoragePool) -> None:
    root = readback(pool, volume_document("kept.qcow2", format_type="qcow2", mode="0640"))
    assert text_at(root, "name") == "kept.qcow2"
    assert find(root, "target/format").get("type") == "qcow2"
    assert text_at(root, "target/permissions/mode") == "0640"


def test_readback_retains_the_submitted_backing_store(pool: FakeStoragePool) -> None:
    root = readback(pool, volume_document(backing_path=f"{POOL_TARGET}/base.qcow2"))
    assert text_at(root, "backingStore/path") == f"{POOL_TARGET}/base.qcow2"
    assert find(root, "backingStore/format").get("type") == "qcow2"


def test_readback_applies_defaults_for_absent_optional_input(pool: FakeStoragePool) -> None:
    root = readback(pool, volume_document(capacity="65536", format_type=None))
    assert find(root, "target/format").get("type") == "raw"
    assert text_at(root, "target/permissions/mode") == "0600"
    assert root.find("backingStore") is None


# --- derived fields -----------------------------------------------------------------------


def test_readback_overrides_the_submitted_root_type(pool: FakeStoragePool) -> None:
    """libvirt takes the type from the pool backend; a submitted 'block' reads back 'file'."""
    assert readback(pool, volume_document(root_type="block")).get("type") == "file"


def test_readback_derives_key_and_path_from_the_pool(pool: FakeStoragePool) -> None:
    volume = pool.createXML(volume_document("disk.qcow2"))
    root = ET.fromstring(volume.XMLDesc(0))
    expected = f"{POOL_TARGET}/disk.qcow2"
    assert text_at(root, "key") == expected
    assert text_at(root, "target/path") == expected
    assert volume.key() == expected
    assert volume.path() == expected


@pytest.mark.parametrize(
    ("unit", "submitted", "expected"),
    [
        ("bytes", 1, 1),
        ("B", 1, 1),
        ("K", 1, 1024),
        ("KiB", 1, 1024),
        ("KB", 1, 1000),
        ("M", 1, 1048576),
        ("MiB", 1, 1048576),
        ("MB", 1, 1000000),
        ("G", 1, 1073741824),
        ("GiB", 1, 1073741824),
        ("GB", 1, 1000000000),
        ("T", 1, 2**40),
        ("TB", 1, 10**12),
        ("P", 1, 2**50),
        ("E", 1, 2**60),
    ],
)
def test_readback_normalises_capacity_to_bytes(
    pool: FakeStoragePool, unit: str, submitted: int, expected: int
) -> None:
    capacity = find(readback(pool, volume_document(capacity=str(submitted), unit=unit)), "capacity")
    assert capacity.get("unit") == "bytes"
    assert capacity.text == str(expected)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("k", 1024), ("kib", 1024), ("Kib", 1024), ("KIB", 1024), ("b", 1), ("Bytes", 1)],
)
def test_readback_matches_capacity_suffixes_case_insensitively(
    pool: FakeStoragePool, unit: str, expected: int
) -> None:
    root = readback(pool, volume_document(capacity="1", unit=unit))
    assert text_at(root, "capacity") == str(expected)


@pytest.mark.parametrize("unit", [None, ""])
def test_readback_treats_an_absent_or_empty_capacity_unit_as_bytes(
    pool: FakeStoragePool, unit: str | None
) -> None:
    """render_volume_xml emits <capacity> with no unit, so this must not raise."""
    capacity = find(readback(pool, volume_document(capacity="4096", unit=unit)), "capacity")
    assert capacity.get("unit") == "bytes"
    assert capacity.text == "4096"


def test_readback_accepts_the_document_render_volume_xml_produces(pool: FakeStoragePool) -> None:
    """The double must not refuse the provider's own output."""
    document = render_volume_xml(
        "overlay.qcow2",
        capacity_bytes=1048576,
        backing_path=f"{POOL_TARGET}/base.qcow2",
        owner_id=64055,
        group_id=108,
    )
    root = readback(pool, document)
    capacity = find(root, "capacity")
    assert capacity.get("unit") == "bytes"
    assert capacity.text == "1048576"
    assert find(root, "target/format").get("type") == "qcow2"
    assert text_at(root, "backingStore/path") == f"{POOL_TARGET}/base.qcow2"


# --- placeholders -------------------------------------------------------------------------


def test_readback_renders_host_facts_as_placeholders(pool: FakeStoragePool) -> None:
    """Real libvirt fills these from the file on disk, so the double states a placeholder."""
    root = readback(pool, volume_document())
    for tag in ("allocation", "physical"):
        assert text_at(root, tag) == "0"
        assert find(root, tag).get("unit") == "bytes"
    assert text_at(root, "target/permissions/owner") == "0"
    assert text_at(root, "target/permissions/group") == "0"
    assert text_at(root, "target/permissions/label") in (None, "")
    for tag in MODELLED_TIMESTAMPS:
        assert text_at(root, f"target/timestamps/{tag}") == "0"


def test_info_reports_capacity_and_placeholder_allocation(pool: FakeStoragePool) -> None:
    """Real libvirt answers info()[2] with the allocation, not the capacity."""
    info = pool.createXML(volume_document(capacity="1048576")).info()
    assert len(info) == 3
    assert info[1] == 1048576
    assert info[2] == 0


# --- refusals -----------------------------------------------------------------------------


def test_create_rejects_a_document_with_no_capacity(pool: FakeStoragePool) -> None:
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.createXML("<volume><name>x.raw</name></volume>")
    assert exc.value.get_error_code() == libvirt.VIR_ERR_XML_ERROR
    assert pool.listVolumes() == []


@pytest.mark.parametrize("unit", ["bogusUnit", " K"])
def test_create_rejects_an_unknown_capacity_unit(pool: FakeStoragePool, unit: str) -> None:
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.createXML(volume_document(unit=unit))
    assert exc.value.get_error_code() == libvirt.VIR_ERR_INVALID_ARG
    assert pool.listVolumes() == []


@pytest.mark.parametrize(
    "document",
    [
        "<volume><capacity unit='bytes'>4096</capacity></volume>",
        "<volume><name></name><capacity unit='bytes'>4096</capacity></volume>",
    ],
)
def test_create_rejects_a_missing_or_empty_name(pool: FakeStoragePool, document: str) -> None:
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.createXML(document)
    assert exc.value.get_error_code() == libvirt.VIR_ERR_XML_ERROR


@pytest.mark.parametrize("capacity", ["abc", "-1"])
def test_create_rejects_malformed_capacity_text(pool: FakeStoragePool, capacity: str) -> None:
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.createXML(volume_document(capacity=capacity))
    assert exc.value.get_error_code() == libvirt.VIR_ERR_XML_ERROR


def test_create_rejects_a_duplicate_volume_name(pool: FakeStoragePool) -> None:
    """ensure_named_overlay guards on this refusal and maps it to PROVISIONING_FAILURE."""
    document = volume_document("dup.raw")
    first = pool.createXML(document)
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.createXML(document)
    assert exc.value.get_error_code() == libvirt.VIR_ERR_STORAGE_VOL_EXIST
    assert pool.storageVolLookupByName("dup.raw") is first


# --- pool behaviour -----------------------------------------------------------------------


def test_lookup_returns_the_created_volume(pool: FakeStoragePool) -> None:
    volume = pool.createXML(volume_document("disk.qcow2"))
    assert pool.storageVolLookupByName("disk.qcow2") is volume
    assert pool.listVolumes() == ["disk.qcow2"]


def test_lookup_raises_no_storage_vol_for_an_unknown_name(pool: FakeStoragePool) -> None:
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.storageVolLookupByName("absent.qcow2")
    assert exc.value.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL


def test_delete_removes_the_volume(pool: FakeStoragePool) -> None:
    pool.createXML(volume_document("gone.qcow2")).delete()
    with pytest.raises(libvirt.libvirtError) as exc:
        pool.storageVolLookupByName("gone.qcow2")
    assert exc.value.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL
    assert pool.listVolumes() == []


def test_created_xml_records_every_submitted_document(pool: FakeStoragePool) -> None:
    first = volume_document("one.qcow2")
    second = volume_document("two.qcow2")
    pool.createXML(first)
    pool.createXML(second)
    assert pool.created_xml == [first, second]


def test_stream_upload_download_and_clone_preserve_exact_bytes(pool: FakeStoragePool) -> None:
    conn = FakeStorageConn(pool)
    source = pool.createXML(volume_document("source.raw", capacity="4"))
    upload = conn.newStream(0)
    source.upload(upload, 0, 4, 0)
    chunks = iter((b"da", b"ta", b""))
    upload.sendAll(lambda _stream, _bound, _opaque: next(chunks), None)
    upload.finish()

    clone = pool.createXMLFrom(volume_document("clone.raw", capacity="4"), source, 0)
    download = conn.newStream(0)
    clone.download(download, 0, 0, 0)
    received: list[bytes] = []
    download.recvAll(lambda _stream, chunk, _opaque: received.append(chunk), None)
    download.finish()

    assert b"".join(received) == b"data"
    assert [volume.name() for volume in pool.listAllVolumes(0)] == ["source.raw", "clone.raw"]


def test_aborted_upload_does_not_publish_partial_bytes(pool: FakeStoragePool) -> None:
    conn = FakeStorageConn(pool)
    volume = pool.createXML(volume_document("source.raw", capacity="4"))
    stream = conn.newStream(0)
    volume.upload(stream, 0, 4, 0)
    chunks = iter((b"part", b""))
    stream.sendAll(lambda _stream, _bound, _opaque: next(chunks), None)
    stream.abort()

    download = conn.newStream(0)
    volume.download(download, 0, 0, 0)
    received: list[bytes] = []
    download.recvAll(lambda _stream, chunk, _opaque: received.append(chunk), None)
    assert received == []
