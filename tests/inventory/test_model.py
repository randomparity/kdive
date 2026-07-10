"""Parse-time validation tests for the systems.toml v2 model (issue #389, Task 1.2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kdive.domain.catalog.images import ImageVisibility
from kdive.inventory.errors import InventoryError
from kdive.inventory.model import (
    BuildSource,
    InventoryDoc,
    S3Source,
    StagedPathSource,
    StagedSource,
)


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 2,
        "image": [
            {
                "provider": "remote-libvirt",
                "name": "base",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": "staged", "volume": "base.qcow2"},
            }
        ],
        "remote_libvirt": [
            {
                "name": "h1",
                "uri": "qemu+tls://h1/system",
                "gdb_addr": "10.0.0.1",
                "gdbstub_range": "47000:47099",
                "client_cert_ref": "c.pem",
                "client_key_ref": "k.pem",  # pragma: allowlist secret - filename ref
                "ca_cert_ref": "ca.pem",  # pragma: allowlist secret - filename ref
                "base_image": "base",
                "cost_class": "remote",
                "concurrent_allocation_cap": 1,
                "vcpus": 8,
                "memory_mb": 16384,
                "shapes": ["small"],
            }
        ],
    }
    base.update(overrides)
    return base


def test_wellformed_parses() -> None:
    doc = InventoryDoc.parse(_doc())
    src = doc.image[0].source
    assert isinstance(src, StagedSource)
    assert src.volume == "base.qcow2"
    assert doc.image[0].visibility is ImageVisibility.PUBLIC
    assert doc.remote_libvirt[0].base_image == "base"


def test_image_rejects_unknown_capability_token() -> None:
    # capabilities is the closed Capability vocabulary (ADR-0286); an off-vocabulary tag is a
    # hard parse error, not a silent passthrough.
    d = _doc()
    d["image"][0]["capabilities"] = ["kdive-ready-console"]
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_image_accepts_known_capability_tokens() -> None:
    d = _doc()
    d["image"][0]["capabilities"] = ["agent", "kdump", "drgn", "build"]
    doc = InventoryDoc.parse(d)
    assert [str(c) for c in doc.image[0].capabilities] == ["agent", "kdump", "drgn", "build"]


def test_image_description_defaults_empty() -> None:
    doc = InventoryDoc.parse(_doc())
    assert doc.image[0].description == ""


def test_image_accepts_description() -> None:
    d = _doc()
    d["image"][0]["description"] = "RHEL debug host with my SLES crash setup"
    doc = InventoryDoc.parse(d)
    assert doc.image[0].description == "RHEL debug host with my SLES crash setup"


def test_image_accepts_max_length_description() -> None:
    d = _doc()
    d["image"][0]["description"] = "z" * 280
    doc = InventoryDoc.parse(d)
    assert len(doc.image[0].description) == 280


def test_image_rejects_overlong_description() -> None:
    # A freeform operator hint is echoed on every images.list row, so it is capped for token
    # safety; an over-long value is a hard parse error naming the image and the limit.
    d = _doc()
    d["image"][0]["description"] = "z" * 281
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_remote_libvirt_requires_size_ceiling() -> None:
    # vcpus/memory_mb are the admission ≤-resource-caps ceiling; remote-libvirt is config-owned,
    # so omitting either is a hard parse error (no host without a grantable ceiling).
    for missing in ("vcpus", "memory_mb"):
        d = _doc()
        del d["remote_libvirt"][0][missing]
        with pytest.raises(InventoryError):
            InventoryDoc.parse(d)


def test_remote_libvirt_size_ceiling_must_be_positive() -> None:
    # gt=0 matches the resources.register_* schema: a non-positive ceiling (e.g. a `vcpus = 0`
    # typo) is rejected at config load, not silently admitted into a host that then rejects every
    # allocation with a misleading "exceeds ceiling 0".
    for bad_field in ("vcpus", "memory_mb"):
        d = _doc()
        d["remote_libvirt"][0][bad_field] = 0
        with pytest.raises(InventoryError):
            InventoryDoc.parse(d)


def test_empty_document_parses() -> None:
    doc = InventoryDoc.parse({"schema_version": 2})
    assert doc.image == []
    assert doc.remote_libvirt == []
    assert doc.local_libvirt == []
    assert doc.fault_inject == []


def test_image_identity_property() -> None:
    doc = InventoryDoc.parse(_doc())
    assert doc.image[0].identity == ("remote-libvirt", "base", "x86_64")


def test_s3_source_with_digest() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "i",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {
                    "kind": "s3",
                    "object_key": "k",
                    "digest": "sha256:ab",
                },
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, S3Source)
    assert src.object_key == "k"
    assert src.digest == "sha256:ab"


def test_s3_source_digest_optional() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "i",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": "s3", "object_key": "k"},
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, S3Source)
    assert src.digest is None


def _s3_image(**over: Any) -> dict[str, Any]:
    img: dict[str, Any] = {
        "provider": "local-libvirt",
        "name": "i",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "s3", "object_key": "k"},
    }
    img.update(over)
    return img


def test_s3_attested_operands_parse() -> None:
    d = _doc(
        image=[_s3_image(attested={"boot_kernel_count": 1, "makedumpfile_version": "1.7.9"})],
        remote_libvirt=[],
    )
    entry = InventoryDoc.parse(d).image[0]
    assert entry.attested is not None
    assert entry.attested.as_provenance() == {
        "boot_kernel_count": 1,
        "makedumpfile_version": "1.7.9",
    }


def test_s3_attested_single_operand_omits_unset() -> None:
    d = _doc(image=[_s3_image(attested={"boot_kernel_count": 1})], remote_libvirt=[])
    entry = InventoryDoc.parse(d).image[0]
    assert entry.attested is not None
    assert entry.attested.as_provenance() == {"boot_kernel_count": 1}


def test_attested_rejected_on_non_s3_source() -> None:
    # A build source owns publish-verified provenance and a staged-path source has its sidecar;
    # attestation would let an operator claim shadow a verified fact (ADR-0323) — reject at load.
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "built",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": "build", "base": "fedora-43"},
                "attested": {"boot_kernel_count": 1},
            }
        ],
        remote_libvirt=[],
    )
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_empty_attested_table_rejected() -> None:
    d = _doc(image=[_s3_image(attested={})], remote_libvirt=[])
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_attested_negative_boot_kernel_count_rejected() -> None:
    d = _doc(image=[_s3_image(attested={"boot_kernel_count": -1})], remote_libvirt=[])
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_build_source() -> None:
    d = _doc(
        image=[
            {
                "provider": "local-libvirt",
                "name": "built",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {
                    "kind": "build",
                    "base": "fedora-43",
                    "components": ["kdump"],
                },
            }
        ],
        remote_libvirt=[],
    )
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, BuildSource)
    assert src.base == "fedora-43"
    assert src.components == ["kdump"]


def _staged_path_image(**over: Any) -> dict[str, Any]:
    img: dict[str, Any] = {
        "provider": "local-libvirt",
        "name": "local-rootfs",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged-path", "path": "/var/lib/kdive/rootfs/local-rootfs.qcow2"},
    }
    img.update(over)
    return img


def test_staged_path_source_parses() -> None:
    d = _doc(image=[_staged_path_image()], remote_libvirt=[])
    src = InventoryDoc.parse(d).image[0].source
    assert isinstance(src, StagedPathSource)
    assert src.path == "/var/lib/kdive/rootfs/local-rootfs.qcow2"


def test_staged_path_rejects_relative_path() -> None:
    d = _doc(
        image=[_staged_path_image(source={"kind": "staged-path", "path": "rootfs/local.qcow2"})],
        remote_libvirt=[],
    )
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_staged_path_rejects_private_visibility() -> None:
    # A private staged-path image would surface to its owning project via images.list yet be
    # unresolvable by the public-scope local catalog lane (ADR-0228) — reject at load.
    d = _doc(image=[_staged_path_image(visibility="private")], remote_libvirt=[])
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_duplicate_image_identity_rejected() -> None:
    img = {
        "provider": "local-libvirt",
        "name": "dup",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(image=[img, dict(img)], remote_libvirt=[]))


def test_same_name_different_arch_is_not_duplicate() -> None:
    # identity is (provider, name, arch); a different arch is a distinct image.
    base = {
        "provider": "local-libvirt",
        "name": "dup",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    d = _doc(
        image=[
            {**base, "arch": "x86_64"},
            {**base, "arch": "aarch64"},
        ],
        remote_libvirt=[],
    )
    doc = InventoryDoc.parse(d)
    assert len(doc.image) == 2


def test_base_image_cross_ref_must_name_declared_image() -> None:
    d = _doc()
    d["remote_libvirt"][0]["base_image"] = "does-not-exist"
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_unknown_source_kind_rejected() -> None:
    d = _doc()
    d["image"][0]["source"] = {"kind": "ftp", "url": "x"}
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_unsupported_image_format_rejected() -> None:
    d = _doc()
    d["image"][0]["format"] = "raw"
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_wrong_schema_version_rejected() -> None:
    d = _doc(schema_version=1)
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    # A pydantic structural failure is re-raised under the generic ('inventory', 'schema')
    # locator with the underlying ValidationError text preserved in the message.
    assert excinfo.value.entry == "inventory"
    assert excinfo.value.field == "schema"
    assert "schema_version" in str(excinfo.value)


def test_duplicate_remote_instance_name_rejected() -> None:
    d = _doc()
    second = dict(d["remote_libvirt"][0])
    d["remote_libvirt"] = [d["remote_libvirt"][0], second]
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    # The duplicate is reported under the offending provider kind, with the colliding name
    # listed in the message so the operator can find it.
    assert excinfo.value.entry == "remote_libvirt"
    assert excinfo.value.field == "name"
    assert "h1" in str(excinfo.value)


def test_duplicate_local_libvirt_name_rejected() -> None:
    inst = {"name": "loc", "cost_class": "local", "host_uri": "qemu:///system"}
    d = _doc(remote_libvirt=[], local_libvirt=[inst, dict(inst)])
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    assert excinfo.value.entry == "local_libvirt"
    assert excinfo.value.field == "name"
    assert "loc" in str(excinfo.value)


def test_multiple_distinct_remote_instances_parse() -> None:
    # ADR-0187 (#395): per-op resource selection is wired, so N remote-libvirt hosts are allowed.
    d = _doc()
    second = {**d["remote_libvirt"][0], "name": "h2", "uri": "qemu+tls://h2/system"}
    first_name = d["remote_libvirt"][0]["name"]
    d["remote_libvirt"] = [d["remote_libvirt"][0], second]
    doc = InventoryDoc.parse(d)
    assert sorted(inst.name for inst in doc.remote_libvirt) == sorted([first_name, "h2"])


def test_duplicate_fault_inject_name_rejected() -> None:
    inst = {
        "name": "fi",
        "cost_class": "local",
        "vcpus": 2,
        "memory_mb": 1024,
    }
    d = _doc(remote_libvirt=[], fault_inject=[inst, dict(inst)])
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_local_libvirt_instance_parses() -> None:
    d = _doc(
        remote_libvirt=[],
        local_libvirt=[
            {
                "name": "loc",
                "cost_class": "local",
                "host_uri": "qemu:///system",
            }
        ],
    )
    doc = InventoryDoc.parse(d)
    assert doc.local_libvirt[0].host_uri == "qemu:///system"
    # guest_egress defaults off (secure default; #1031/ADR-0313) — a block omitting the key
    # keeps restrict=on behavior.
    assert doc.local_libvirt[0].guest_egress is False


def test_local_libvirt_guest_egress_opt_in_parses() -> None:
    d = _doc(
        remote_libvirt=[],
        local_libvirt=[
            {
                "name": "loc",
                "cost_class": "local",
                "host_uri": "qemu:///system",
                "guest_egress": True,
            }
        ],
    )
    doc = InventoryDoc.parse(d)
    assert doc.local_libvirt[0].guest_egress is True


def test_missing_required_field_rejected() -> None:
    d = _doc()
    del d["image"][0]["root_device"]
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


_SOURCE_STRATEGY = st.one_of(
    st.fixed_dictionaries(
        {
            "kind": st.just("staged"),
            "volume": st.text(min_size=1, max_size=20),
        }
    ),
    st.fixed_dictionaries(
        {
            "kind": st.just("s3"),
            "object_key": st.text(min_size=1, max_size=20),
        }
    ),
    st.fixed_dictionaries(
        {
            "kind": st.just("build"),
            "base": st.text(min_size=1, max_size=20),
        }
    ),
)


@given(source=_SOURCE_STRATEGY)
def test_source_union_discriminates_on_kind(source: dict[str, Any]) -> None:
    d = _doc(
        image=[
            {
                "provider": "p",
                "name": "n",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": source,
            }
        ],
        remote_libvirt=[],
    )
    doc = InventoryDoc.parse(d)
    assert doc.image[0].source.kind == source["kind"]


@given(kind=st.text(min_size=1, max_size=8).filter(lambda k: k not in {"s3", "build", "staged"}))
def test_unknown_discriminator_always_raises_inventory_error(kind: str) -> None:
    d = _doc(
        image=[
            {
                "provider": "p",
                "name": "n",
                "arch": "x86_64",
                "format": "qcow2",
                "root_device": "/dev/vda",
                "visibility": "public",
                "source": {"kind": kind, "x": "y"},
            }
        ],
        remote_libvirt=[],
    )
    with pytest.raises(InventoryError):
        InventoryDoc.parse(d)


def test_cross_ref_error_preserves_precise_entry_and_field() -> None:
    # A semantic failure must surface its precise entry/field, not be flattened to
    # the generic ('inventory', 'schema') locator a pydantic after-validator would force.
    d = _doc()
    d["remote_libvirt"][0]["base_image"] = "nope"
    try:
        InventoryDoc.parse(d)
    except InventoryError as exc:
        assert exc.entry == "remote_libvirt[h1]"
        assert exc.field == "base_image"
        assert "nope" in str(exc)
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")


def test_duplicate_identity_error_preserves_precise_entry_and_field() -> None:
    img = {
        "provider": "local-libvirt",
        "name": "dup",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    try:
        InventoryDoc.parse(_doc(image=[img, dict(img)], remote_libvirt=[]))
    except InventoryError as exc:
        assert exc.entry == "image[dup]"
        assert exc.field == "identity"
        # The colliding identity tuple is named in the message.
        assert "('local-libvirt', 'dup', 'x86_64')" in str(exc)
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")


def test_duplicate_instance_name_error_preserves_kind_and_field() -> None:
    inst = {"name": "fi", "cost_class": "local", "vcpus": 2, "memory_mb": 1024}
    try:
        InventoryDoc.parse(_doc(remote_libvirt=[], fault_inject=[inst, dict(inst)]))
    except InventoryError as exc:
        assert exc.entry == "fault_inject"
        assert exc.field == "name"
        # The colliding name(s) are listed in the message, not dropped.
        assert "fi" in str(exc)
    else:  # pragma: no cover - parse must raise
        pytest.fail("expected InventoryError")


def test_cost_class_block_parses() -> None:
    d = _doc(cost_class=[{"name": "premium", "coeff": 2.5}])
    doc = InventoryDoc.parse(d)
    assert doc.cost_class[0].name == "premium"
    assert doc.cost_class[0].coeff == Decimal("2.5")


def test_cost_class_coeff_uses_decimal_string_construction() -> None:
    # A TOML float 0.1 must land as Decimal("0.1"), not the binary-float expansion.
    doc = InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": 0.1}]))
    assert doc.cost_class[0].coeff == Decimal("0.1")


def test_cost_class_absent_defaults_empty() -> None:
    assert InventoryDoc.parse(_doc()).cost_class == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_cost_class_blank_name_rejected(bad: str) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": bad, "coeff": 1.0}]))


@pytest.mark.parametrize("bad", [0, -1, "0", "-2"])
def test_cost_class_non_positive_coeff_rejected(bad: object) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": bad}]))


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_cost_class_non_finite_coeff_rejected(bad: str) -> None:
    with pytest.raises(InventoryError):
        InventoryDoc.parse(_doc(cost_class=[{"name": "c", "coeff": bad}]))


def test_duplicate_cost_class_name_rejected() -> None:
    d = _doc(cost_class=[{"name": "dup", "coeff": 1.0}, {"name": "dup", "coeff": 2.0}])
    with pytest.raises(InventoryError) as excinfo:
        InventoryDoc.parse(d)
    assert excinfo.value.entry == "cost_class"
    assert excinfo.value.field == "name"
    assert "dup" in str(excinfo.value)
