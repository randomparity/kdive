"""``systems.profile_examples`` — discoverable, schema-and-policy-valid example profiles (#451).

The tool projects the ``systems.toml`` inventory into one ready-to-edit example profile per
configured provider (ADR-0124). The tests assert three contracts:

1. **Validity** — every emitted example, *as emitted* (real ref or placeholder, no edits), parses
   via ``ProvisioningProfile.parse()`` and passes ``validate_profile_for_provider()`` (schema +
   provider policy). This is what stops the advertised examples rotting.
2. **No-leak** — no example contains a ``[[remote_libvirt]]`` ``uri``/``gdb_addr``/``gdbstub_range``
   or a ``*_cert_ref`` secret-ref name, and no ``private``-visibility inventory image name appears.
3. **Shape** — one item per configured provider; placeholders carry a ``note``; the collection
   chains into ``systems.define``/``allocations.request``.

The validity test must reckon with a file-vs-doc coupling: ``validate_rootfs_reference`` re-loads
the inventory from ``KDIVE_SYSTEMS_TOML`` (not the in-memory doc), so the test points that env at
the same temp file both the builder and the validator read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import kdive.config as config
from kdive.config.core_settings import SYSTEMS_TOML
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.mcp.tools.lifecycle.systems.profile_examples import build_profile_examples
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.composition import _component_sources
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import validate_profile_for_provider

_SENSITIVE_TOKENS = ("uri", "gdb_addr", "gdbstub_range", "cert_ref")

_FULL_INVENTORY = """
schema_version = 2

[[image]]
provider = "local-libvirt"
name = "fedora-public"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda1"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-public.qcow2"

[[image]]
provider = "local-libvirt"
name = "secret-private"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda1"
visibility = "private"
[image.source]
kind = "staged"
volume = "secret-private.qcow2"

[[image]]
provider = "remote-libvirt"
name = "remote-base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda1"
visibility = "public"
[image.source]
kind = "staged"
volume = "remote-base.qcow2"

[[local_libvirt]]
name = "local-host"
cost_class = "local"
host_uri = "qemu:///system"

[[remote_libvirt]]
name = "remote-host"
cost_class = "remote"
uri = "qemu+tls://internal.example.com/system"
gdb_addr = "10.0.0.5"
gdbstub_range = "1234-1240"
client_cert_ref = "secret://client-cert"  # pragma: allowlist secret
client_key_ref = "secret://client-key"  # pragma: allowlist secret
ca_cert_ref = "secret://ca-cert"  # pragma: allowlist secret
base_image = "remote-base"
vcpus = 4
memory_mb = 8192

[[fault_inject]]
name = "fi-host"
cost_class = "fault"
vcpus = 2
memory_mb = 2048
"""


def _write_inventory(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(body)
    return path


def _examples(doc: InventoryDoc | None) -> dict[str, dict[str, Any]]:
    resp = build_profile_examples(doc)
    return {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}


def _profile_of(item_data: dict[str, Any]) -> dict[str, Any]:
    profile = item_data["profile"]
    assert isinstance(profile, dict)
    return profile


def _validate(provider: str, profile: dict[str, Any]) -> None:
    parsed = ProvisioningProfile.parse(profile)
    if provider == "local-libvirt":
        validate_profile_for_provider(parsed, LocalLibvirtProfilePolicy(), _component_sources())
    elif provider == "fault-inject":
        # fault-inject owns no rootfs/component sources; its policy is a no-op validate.
        FaultInjectProfilePolicy().validate_profile(parsed)
    # remote-libvirt has no static rootfs/policy check at this layer; parse() is the gate.


def test_one_example_per_configured_provider(tmp_path: Path) -> None:
    doc = InventoryDoc.parse({"schema_version": 2, "local_libvirt": [], "remote_libvirt": []})
    # A doc with no instances configures no provider → default placeholder set (all three kinds).
    examples = _examples(doc)
    assert set(examples) == {"local-libvirt", "remote-libvirt", "fault-inject"}


def test_full_inventory_examples_are_valid_and_use_real_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_inventory(tmp_path, _FULL_INVENTORY)
    monkeypatch.setenv(SYSTEMS_TOML.name, str(path))
    config.reset()
    doc = load_inventory_optional(path)
    assert doc is not None
    examples = _examples(doc)
    for provider, data in examples.items():
        profile = _profile_of(data)
        _validate(provider, profile)  # parses + passes provider policy as emitted
    # Real refs used where the inventory supplies them.
    local_rootfs = _profile_of(examples["local-libvirt"])["provider"]["local-libvirt"]["rootfs"]
    assert local_rootfs == {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-public"}
    remote = _profile_of(examples["remote-libvirt"])["provider"]["remote-libvirt"]
    assert remote["base_image_volume"] == "remote-base"


def test_placeholder_examples_are_still_valid_when_no_public_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # local-libvirt configured but with NO public image: the example must fall back to a `local`
    # rootfs (not a placeholder catalog name, which would fail validate_rootfs_reference when a
    # file is present), and still parse + pass policy.
    body = """
schema_version = 2
[[local_libvirt]]
name = "local-host"
cost_class = "local"
host_uri = "qemu:///system"
"""
    path = _write_inventory(tmp_path, body)
    monkeypatch.setenv(SYSTEMS_TOML.name, str(path))
    config.reset()
    doc = load_inventory_optional(path)
    examples = _examples(doc)
    local = _profile_of(examples["local-libvirt"])
    rootfs = local["provider"]["local-libvirt"]["rootfs"]
    assert rootfs["kind"] == "local"  # not a placeholder catalog name
    assert rootfs["path"].startswith("/")
    assert examples["local-libvirt"]["note"]  # caller is told to replace it
    _validate("local-libvirt", local)


def test_no_inventory_file_yields_default_placeholder_set() -> None:
    examples = _examples(None)
    assert set(examples) == {"local-libvirt", "remote-libvirt", "fault-inject"}
    for provider, data in examples.items():
        _validate(provider, _profile_of(data))
        assert data["note"]


def test_examples_never_leak_sensitive_inventory_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_inventory(tmp_path, _FULL_INVENTORY)
    monkeypatch.setenv(SYSTEMS_TOML.name, str(path))
    config.reset()
    doc = load_inventory_optional(path)
    blob = json.dumps(_examples(doc))
    for token in _SENSITIVE_TOKENS:
        assert token not in blob.lower(), token
    # The internal hostname / IP / secret-ref values never appear.
    for value in ("internal.example.com", "10.0.0.5", "secret://"):
        assert value not in blob
    # The private image name is never surfaced.
    assert "secret-private" not in blob


def test_collection_chains_into_define(tmp_path: Path) -> None:
    resp = build_profile_examples(None)
    assert "systems.define" in resp.suggested_next_actions
    assert "allocations.request" in resp.suggested_next_actions
