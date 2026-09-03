"""Compare the storage double's readback with a real libvirt one (#2164).

This is the test that makes the unit proof trustworthy. Without it the modelled set is one
author's transcription of one readback, and nothing notices when a libvirt upgrade changes it.

Two invariants this module holds, both learned the hard way:

**The overlay document is built by calling ``render_volume_xml``, never hand-written.** The
renderer emits ``<capacity>`` with no ``unit`` attribute, and a design that only ever tested
hand-written documents carrying an explicit unit did not notice that the double would have
refused the provider's own output. Driving the real producer is the input-side twin of driving
the real entry point. A later contributor reaching for a hand-built string reintroduces exactly
the defect this file exists to catch.

**The base document submits a non-default value for every platform-determined field compared.**
The overlay comes from ``render_volume_xml``, which submits no ``type``, no ``unit``, and no
``permissions``, so without the base document each compared field would carry libvirt's default
on both sides and a double that echoed its input would agree by accident. That accidental
agreement is the exact failure class this issue exists to eliminate, so a comparison that cannot
fail is not a proof.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import libvirt
import pytest

from kdive.providers.remote_libvirt.lifecycle.xml import render_volume_xml
from tests.live_vm import require_live_vm_storage_double
from tests.providers.remote_libvirt.fakes import FakeStoragePool

pytestmark = [pytest.mark.live_vm]

# libvirt overrides or normalises all three, so an echoing double disagrees on all three.
BASE_DOCUMENT = (
    "<volume type='block'><name>base.qcow2</name>"
    "<capacity unit='KiB'>1024</capacity>"
    "<target><format type='qcow2'/><permissions><mode>0640</mode></permissions></target>"
    "</volume>"
)


def child_tags(element: ET.Element) -> tuple[str, ...]:
    return tuple(child.tag for child in element)


def require(element: ET.Element | None, path: str) -> ET.Element:
    assert element is not None, f"readback is missing {path!r}"
    return element


def add_unmodelled_noise(document: str) -> str:
    """Append the children and attribute libvirt accepts and then discards."""
    root = ET.fromstring(document)
    ET.SubElement(ET.SubElement(root, "metadata"), "owner").text = "run-1"
    ET.SubElement(root, "bogusElement").text = "zzz"
    require(root.find("name"), "name").set("kdive", "owned")
    return ET.tostring(root, encoding="unicode")


def assert_branches_agree(real: ET.Element, fake: ET.Element, tag: str) -> None:
    """Compare a target or backingStore branch: tag structure, then the values libvirt fixes."""
    real_branch = require(real.find(tag), tag)
    fake_branch = require(fake.find(tag), tag)
    assert child_tags(real_branch) == child_tags(fake_branch)
    assert child_tags(require(real_branch.find("timestamps"), "timestamps")) == child_tags(
        require(fake_branch.find("timestamps"), "timestamps")
    )
    # `label` carries the file's security label, so a runner without SELinux emits three children
    # where an SELinux host emits four. Only `label` is optional; the subset leg on the unstripped
    # sets keeps the double from rendering a child libvirt never emits.
    real_permissions = set(child_tags(require(real_branch.find("permissions"), "permissions")))
    fake_permissions = set(child_tags(require(fake_branch.find("permissions"), "permissions")))
    assert real_permissions - {"label"} == fake_permissions - {"label"}
    assert real_permissions <= fake_permissions


def assert_readbacks_agree(real_desc: str, fake_desc: str) -> None:
    real = ET.fromstring(real_desc)
    fake = ET.fromstring(fake_desc)
    assert real.tag == fake.tag
    assert real.get("type") == fake.get("type")
    # Distinguishes the two volumes: only the overlay carries backingStore.
    assert child_tags(real) == child_tags(fake)

    assert_branches_agree(real, fake, "target")
    if real.find("backingStore") is not None:
        assert_branches_agree(real, fake, "backingStore")

    # The platform-determined values. These are fixed by libvirt's own rules rather than by the
    # host, so they are identical on every runner — and they are the class where a double most
    # easily agrees with its input instead of the platform. A tag-only comparison would pass a
    # double that echoed a submitted unit='KiB' straight back.
    real_capacity = require(real.find("capacity"), "capacity")
    fake_capacity = require(fake.find("capacity"), "capacity")
    assert real_capacity.text == fake_capacity.text
    assert real_capacity.get("unit") == fake_capacity.get("unit")
    assert require(real.find("target/format"), "target/format").get("type") == require(
        fake.find("target/format"), "target/format"
    ).get("type")
    assert (
        require(real.find("target/permissions/mode"), "mode").text
        == require(fake.find("target/permissions/mode"), "mode").text
    )


def assert_no_unmodelled_content(desc: str) -> None:
    """Walk the parsed tree; never substring-match the readback string.

    Every readback carries the pool target path three times, and ``tmp_path`` honours ``TMPDIR``
    and ``--basetemp``, so any substring check over the whole document false-reds on a runner
    whose temp root happens to contain the token — which this reviewer's own probe pool did for
    ``kdive``. That applies to the payload values too: libvirt derives the volume path *from* the
    pool target, so ``run-1`` or ``zzz`` inside ``TMPDIR`` would land in a readback where the
    double and libvirt agree perfectly. Comparing each element's text for **equality** keeps the
    check — no element carries the payload as its value — with no such failure mode, because a
    path libvirt generates is never exactly one of these tokens.
    """
    payloads = {"run-1", "zzz"}
    for element in ET.fromstring(desc).iter():
        assert element.tag not in ("metadata", "bogusElement")
        assert "kdive" not in element.attrib
        assert (element.text or "").strip() not in payloads
        assert payloads.isdisjoint(element.attrib.values())


def test_double_and_libvirt_agree_on_the_dir_pool_volume_readback(tmp_path: Path) -> None:
    contract = require_live_vm_storage_double()
    # Bound before the try so a failure in storagePoolDefineXML cannot make the finally raise
    # UnboundLocalError over the libvirt error that explains what went wrong.
    conn = None
    pool = None
    try:
        conn = libvirt.open(contract.libvirt_uri)
        pool_name = f"kdive-fidelity-{uuid4().hex}"
        pool = conn.storagePoolDefineXML(
            f"<pool type='dir'><name>{pool_name}</name>"
            f"<target><path>{tmp_path}</path></target></pool>",
            0,
        )
        pool.create(0)

        real_base = pool.createXML(BASE_DOCUMENT, 0)
        # Built by the production renderer, then given the noise libvirt discards.
        overlay_document = add_unmodelled_noise(
            render_volume_xml(
                "overlay.qcow2", capacity_bytes=1048576, backing_path=real_base.path()
            )
        )
        real_overlay_desc = pool.createXML(overlay_document, 0).XMLDesc(0)
        real_base_desc = real_base.XMLDesc(0)

        fake_pool = FakeStoragePool(target_path=str(tmp_path))
        fake_base_desc = fake_pool.createXML(BASE_DOCUMENT).XMLDesc(0)
        fake_overlay_desc = fake_pool.createXML(overlay_document).XMLDesc(0)

        # The base pair is the discriminating one: it is the document that submits non-defaults.
        assert_readbacks_agree(real_base_desc, fake_base_desc)
        assert_readbacks_agree(real_overlay_desc, fake_overlay_desc)

        for desc in (real_base_desc, fake_base_desc, real_overlay_desc, fake_overlay_desc):
            assert_no_unmodelled_content(desc)
    finally:
        if pool is not None:
            # Each step is guarded so an already-absent object does not mask the real failure.
            with suppress(libvirt.libvirtError):
                for name in pool.listVolumes():
                    with suppress(libvirt.libvirtError):
                        pool.storageVolLookupByName(name).delete(0)
            for step in (pool.destroy, pool.undefine):
                with suppress(libvirt.libvirtError):
                    step()
        if conn is not None:
            conn.close()
