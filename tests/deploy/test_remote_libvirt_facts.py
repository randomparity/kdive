"""Structural checks on the rendered systems.toml block template.

Mirrors tests/deploy/test_systemd_units.py: read the Jinja2 source as text and
assert every required field token is present, so the test needs no ansible or
jinja2 runtime. The field set is locked to systems.toml.example (schema v2).
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "ansible"
    / "roles"
    / "remote_libvirt_facts"
    / "templates"
    / "systems_toml_block.j2"
)

REMOTE_LIBVIRT_FIELDS = (
    "name",
    "uri",
    "gdb_addr",
    "gdbstub_range",
    "client_cert_ref",
    "client_key_ref",
    "ca_cert_ref",
    "base_image",
    "cost_class",
    "concurrent_allocation_cap",
    "vcpus",
    "memory_mb",
    "shapes",
)

IMAGE_FIELDS = (
    "provider",
    "arch",
    "format",
    "root_device",
    "visibility",
)


def test_template_has_both_blocks() -> None:
    text = TEMPLATE.read_text()
    assert "[[remote_libvirt]]" in text
    assert "[[image]]" in text
    assert 'kind = "staged"' in text
    assert "volume" in text


def test_remote_libvirt_block_has_all_fields() -> None:
    text = TEMPLATE.read_text()
    for field in REMOTE_LIBVIRT_FIELDS:
        assert f"{field} =" in text, f"missing remote_libvirt field: {field}"


def test_image_block_has_all_fields() -> None:
    text = TEMPLATE.read_text()
    for field in IMAGE_FIELDS:
        assert f"{field} =" in text, f"missing image field: {field}"


def test_no_host_to_controller_fetch_marker() -> None:
    # The facts template must never reference a fetched client bundle path.
    text = TEMPLATE.read_text()
    assert "fetch" not in text.lower()
