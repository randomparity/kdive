"""Loader fault-isolation tests for systems.toml (issue #389, Task 1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory, load_inventory_optional
from kdive.inventory.model import InventoryDoc, StagedPathSource

GOOD = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
"""

BAD_TOML = "schema_version = 2\n[[image]\n"  # malformed table header

BAD_SCHEMA = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "ftp"
url = "x"
"""

REMOTE_LIBVIRT = {
    "name": "remote-a",
    "uri": "qemu+tls://remote-a.example/system",
    "gdb_addr": "192.0.2.10",
    "gdbstub_range": "47000:47099",
    "client_cert_ref": "remote/client-cert",
    "client_key_ref": "remote/client-key",  # pragma: allowlist secret
    "ca_cert_ref": "remote/ca",
    "base_image": "base",
    "cost_class": "remote",
    "vcpus": 4,
    "memory_mb": 4096,
}

AUTHORITY_BINDING = {
    "authority_instance": "authority-a",
    "authority_address": "192.0.2.20",
    "authority_port": 47001,
    "authority_server_ca_ref": "authority/server-ca",
    "authority_client_cert_ref": "authority/client-cert",
    "authority_client_key_ref": "authority/client-key",  # pragma: allowlist secret
}


def _inventory_with_remote(instance: dict[str, object]) -> dict[str, object]:
    return {
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
        "remote_libvirt": [instance],
    }


def test_inventory_error_records_entry_field_and_message() -> None:
    err = InventoryError("image[base]", "base_image", "missing volume")
    assert err.entry == "image[base]"
    assert err.field == "base_image"
    assert str(err) == "image[base].base_image: missing volume"


def test_load_good(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory(p)
    assert doc.image[0].name == "base"


def test_malformed_toml_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError) as excinfo:
        load_inventory(p)
    err = excinfo.value
    assert err.entry == str(p)
    assert err.field == "toml"
    assert str(err).startswith(f"{p}.toml: malformed:")


def test_schema_failure_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_SCHEMA)
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_missing_file_raises_inventory_error(tmp_path: Path) -> None:
    # An explicitly-named path that is absent IS an error.
    absent = tmp_path / "absent.toml"
    with pytest.raises(InventoryError) as excinfo:
        load_inventory(absent)
    err = excinfo.value
    assert err.entry == str(absent)
    assert err.field == "file"
    assert str(err).startswith(f"{absent}.file: cannot read:")


def test_non_utf8_file_raises_inventory_error(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_bytes(b"\xff\xfe schema_version = 2\n")
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_load_optional_returns_none_for_absent_path(tmp_path: Path) -> None:
    # The DEFAULT-path case: an absent file means "nothing declared", not an error.
    assert load_inventory_optional(tmp_path / "absent.toml") is None


def test_load_optional_parses_present_good_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory_optional(p)
    assert doc is not None
    assert doc.image[0].name == "base"


def test_load_optional_still_raises_on_present_malformed_file(tmp_path: Path) -> None:
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError):
        load_inventory_optional(p)


@pytest.mark.parametrize(
    ("authority_fields", "expected_address"),
    [
        ({}, None),
        (AUTHORITY_BINDING, "192.0.2.20"),
    ],
    ids=["absent", "complete"],
)
def test_remote_authority_binding_is_all_absent_or_complete(
    authority_fields: dict[str, object], expected_address: str | None
) -> None:
    instance = {**REMOTE_LIBVIRT, **authority_fields}
    doc = InventoryDoc.parse(_inventory_with_remote(instance))
    assert doc.remote_libvirt[0].authority_address == expected_address


@pytest.mark.parametrize("missing", sorted(AUTHORITY_BINDING))
def test_remote_authority_binding_rejects_partial_tuple(missing: str) -> None:
    authority_fields = dict(AUTHORITY_BINDING)
    del authority_fields[missing]
    with pytest.raises(InventoryError, match="authority binding fields"):
        InventoryDoc.parse(_inventory_with_remote({**REMOTE_LIBVIRT, **authority_fields}))


@pytest.mark.parametrize(
    "address",
    [
        "::1",
        "authority.example",
        "qemu+tls://authority.example/system",
        "192.0.2.020",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_remote_authority_binding_rejects_noncanonical_or_unsafe_destination(address: str) -> None:
    with pytest.raises(InventoryError, match="authority_address"):
        InventoryDoc.parse(
            _inventory_with_remote(
                {**REMOTE_LIBVIRT, **AUTHORITY_BINDING, "authority_address": address}
            )
        )


@pytest.mark.parametrize("port", [0, 65536, "47001"])
def test_remote_authority_binding_rejects_invalid_port(port: object) -> None:
    with pytest.raises(InventoryError, match="authority_port"):
        InventoryDoc.parse(
            _inventory_with_remote({**REMOTE_LIBVIRT, **AUTHORITY_BINDING, "authority_port": port})
        )


@pytest.mark.parametrize(
    "field",
    [
        "authority_instance",
        "authority_server_ca_ref",
        "authority_client_cert_ref",
        "authority_client_key_ref",
    ],
)
def test_remote_authority_binding_rejects_empty_identity_and_secret_refs(field: str) -> None:
    with pytest.raises(InventoryError, match=field):
        InventoryDoc.parse(
            _inventory_with_remote({**REMOTE_LIBVIRT, **AUTHORITY_BINDING, field: " "})
        )


def test_remote_authority_binding_rejects_duplicate_secret_refs() -> None:
    with pytest.raises(InventoryError, match="distinct"):
        InventoryDoc.parse(
            _inventory_with_remote(
                {
                    **REMOTE_LIBVIRT,
                    **AUTHORITY_BINDING,
                    "authority_client_key_ref": AUTHORITY_BINDING["authority_client_cert_ref"],
                }
            )
        )


def test_remote_authority_binding_rejects_extra_fields() -> None:
    with pytest.raises(InventoryError, match="extra_forbidden"):
        InventoryDoc.parse(
            _inventory_with_remote(
                {**REMOTE_LIBVIRT, **AUTHORITY_BINDING, "authority_uri": "tcp://x"}
            )
        )


def test_repo_systems_toml_example_parses_with_staged_path_image() -> None:
    # The shipped reference inventory must stay parseable, and its local-libvirt staged-path
    # image (the host-shell-free discovery path, ADR-0228) must be present and absolute.
    example = Path(__file__).resolve().parents[2] / "systems.toml.example"
    doc = load_inventory(example)
    staged_path = [img for img in doc.image if isinstance(img.source, StagedPathSource)]
    assert staged_path, "systems.toml.example must declare a staged-path local-libvirt image"
    img = staged_path[0]
    assert img.provider == "local-libvirt"
    assert isinstance(img.source, StagedPathSource)
    assert img.source.path.startswith("/var/lib/kdive/rootfs/")


def test_repo_systems_toml_example_declares_ppc64le_baseline_image() -> None:
    # The ppc64le baseline seed row (#1144, epic #1139): the operator template must carry a
    # ppc64le sibling of fedora-kdive-ready-44 so a ppc64le System can resolve an image_catalog
    # row. arch=ppc64le is the identity component that distinguishes it from the x86_64 row.
    example = Path(__file__).resolve().parents[2] / "systems.toml.example"
    doc = load_inventory(example)
    ppc = [img for img in doc.image if img.name == "fedora-kdive-ready-44-ppc64le"]
    assert ppc, "systems.toml.example must declare the fedora-kdive-ready-44-ppc64le image"
    img = ppc[0]
    assert img.provider == "local-libvirt"
    assert img.arch == "ppc64le"
    # It is an externally-baked s3 image with operator-attested operands (ADR-0323): the attested
    # boot_kernel_count keeps the single-kernel baseline provisionable at describe-time.
    assert img.attested is not None
    assert img.attested.boot_kernel_count == 1


def test_repo_systems_toml_example_uses_only_known_capability_tokens() -> None:
    # Every capability tag in the shipped inventory must be a member of the closed vocabulary
    # (ADR-0286); no off-vocabulary `kdive-ready-console`/`ssh`/`cloud-init` tokens.
    example = Path(__file__).resolve().parents[2] / "systems.toml.example"
    doc = load_inventory(example)
    known = {c.value for c in Capability}
    for img in doc.image:
        for cap in img.capabilities:
            assert cap in known, f"unknown capability token {cap!r} in systems.toml.example"
