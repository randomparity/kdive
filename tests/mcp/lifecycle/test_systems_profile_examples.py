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
from kdive.domain.catalog.resources import ResourceKind
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.mcp.tools.lifecycle.systems.profile_examples import build_profile_examples
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.composition import _component_sources
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import validate_profile_for_provider

_SENSITIVE_TOKENS = ("uri", "gdb_addr", "gdbstub_range", "cert_ref")

_LOCAL = ResourceKind.LOCAL_LIBVIRT
_REMOTE = ResourceKind.REMOTE_LIBVIRT
_FAULT = ResourceKind.FAULT_INJECT

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


def _examples(
    doc: InventoryDoc | None,
    kinds: frozenset[ResourceKind] = frozenset(ResourceKind),
) -> dict[str, dict[str, Any]]:
    resp = build_profile_examples(doc, kinds)
    return {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}


def _providers(resp) -> set[str]:  # type: ignore[type-arg]
    """Provider aliases from a build_profile_examples response."""
    return {cast(dict[str, Any], item.data)["provider"] for item in resp.items}


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
    # kinds drives provider selection; passing all three yields one example per provider.
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
    # base_image_volume is the operator-staged libvirt *volume* (rootfs_build.py), which the
    # provider looks up by name — not the catalog image name. The example must emit a value that
    # actually resolves on the host: the staged source's volume (`remote-base.qcow2`), not
    # `remote-base`. Emitting the bare name leaves provisioning to fail "base image not staged".
    assert remote["base_image_volume"] == "remote-base.qcow2"


_STAGED_PATH_INVENTORY = """
schema_version = 2

[[image]]
provider = "local-libvirt"
name = "fedora-local"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda1"
visibility = "public"
[image.source]
kind = "staged-path"
path = "/var/lib/kdive/rootfs/fedora-local.qcow2"
"""


def test_local_example_uses_catalog_ref_for_public_staged_path_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A public staged-path image (ADR-0228) makes the local example a real `catalog` ref, just
    # like any other public source kind — profile_examples is source-kind-agnostic. The path
    # itself is never emitted into the example.
    path = _write_inventory(tmp_path, _STAGED_PATH_INVENTORY)
    monkeypatch.setenv(SYSTEMS_TOML.name, str(path))
    config.reset()
    doc = load_inventory_optional(path)
    assert doc is not None
    data = _examples(doc)["local-libvirt"]
    rootfs = _profile_of(data)["provider"]["local-libvirt"]["rootfs"]
    assert rootfs == {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-local"}
    assert data["uses_real_reference"] is True
    assert "/var/lib/kdive/rootfs/fedora-local.qcow2" not in str(data)


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


def test_collection_chains_full_discovery_lifecycle(tmp_path: Path) -> None:
    # #474: the entry breadcrumb must walk a cold agent through discovery → provision so an agent
    # that follows suggested_next_actions reaches a granted allocation on the first valid attempt.
    resp = build_profile_examples(None, frozenset(ResourceKind))
    actions = resp.suggested_next_actions
    # The discovery tools that build a valid request appear, in order, before allocations.request.
    discovery = ("resources.list", "shapes.list", "accounting.estimate")
    for tool in discovery:
        assert tool in actions, tool
    discovery_order = [actions.index(t) for t in discovery]
    assert discovery_order == sorted(discovery_order)
    assert actions.index("accounting.estimate") < actions.index("allocations.request")
    # The lifecycle continues into provision/get/teardown/release.
    for tool in ("systems.provision", "systems.get", "systems.teardown", "allocations.release"):
        assert tool in actions, tool


def test_direct_kernel_placeholder_is_a_non_uri_warm_tree_label() -> None:
    # D5 (#763): the direct-kernel placeholder kernel_source_ref must be a bare warm-tree label,
    # not a URI-looking string. A `git:…`/`https://…`-looking bare string is silently routed to
    # the local warm-tree lane (workspace.real_checkout dispatches on the {"git": {...}} *object*,
    # never on a string scheme), so advertising one teaches the misleading shape the
    # build-source-staging doc warns about. Mirror the sibling runs.profile_examples placeholder
    # (`REPLACE_ME-warm-tree-source`), which is already a non-URI label.
    examples = _examples(None)
    for provider in ("local-libvirt", "fault-inject"):
        ref = _profile_of(examples[provider])["kernel_source_ref"]
        assert isinstance(ref, str)
        assert "REPLACE_ME" in ref
        # No URI-looking scheme prefix: a bare `git:`/`https:` etc. is the trap.
        assert "://" not in ref
        assert not any(ref.startswith(f"{scheme}:") for scheme in ("git", "https", "http", "ssh"))


def test_disk_image_example_emits_no_kernel_source_ref(tmp_path: Path) -> None:
    # #472: the remote-libvirt (disk-image) example must not instruct the agent to invent a kernel
    # source for a VM-only provision.
    examples = _examples(None)
    remote = _profile_of(examples["remote-libvirt"])
    assert "kernel_source_ref" not in remote
    # The direct-kernel examples still carry the learnable build source.
    for provider in ("local-libvirt", "fault-inject"):
        assert "kernel_source_ref" in _profile_of(examples[provider]), provider


def test_examples_carry_sizing_note_and_concrete_size() -> None:
    # #461: the example carries concrete sizing (so it parses alone and provisions a full-custom
    # allocation as-is) AND a sizing_note telling the caller to omit/match for a shape-sized
    # allocation, whose resolved size would otherwise conflict.
    for data in _examples(None).values():
        profile = _profile_of(data)
        for field in ("vcpu", "memory_mb", "disk_gb"):
            assert isinstance(profile[field], int)
        sizing_note = data["sizing_note"]
        assert "omit" in sizing_note.lower()
        assert "shape" in sizing_note.lower()


def test_local_example_carries_debug_block_and_note() -> None:
    # #1014 (BLACK_BOX_REVIEW.md Finding 3(a)): the local-libvirt example must surface the
    # provision-bound debug flags and tell the caller they cannot be added after provisioning.
    examples = _examples(None)
    local = _profile_of(examples["local-libvirt"])
    debug = local["provider"]["local-libvirt"]["debug"]
    assert debug == {"gdbstub": False, "preserve_on_crash": False}
    note = examples["local-libvirt"]["note"].lower()
    assert "debug" in note
    assert "gdbstub" in note
    assert "provision" in note


def test_remote_and_fault_examples_carry_no_debug_block() -> None:
    # remote-libvirt's gdbstub is unconditional (no flag to set) and fault-inject owns no
    # crash-capture flags at all, so neither profile section declares a `debug` key.
    examples = _examples(None)
    remote = _profile_of(examples["remote-libvirt"])["provider"]["remote-libvirt"]
    fault = _profile_of(examples["fault-inject"])["provider"]["fault-inject"]
    assert "debug" not in remote
    assert "debug" not in fault


def test_collection_and_item_status_are_ok() -> None:
    # The collection and each item report the literal "ok" status (a read-only discovery success).
    resp = build_profile_examples(None, frozenset(ResourceKind))
    assert resp.status == "ok"
    assert resp.object_id == "profile-examples"
    assert [item.status for item in resp.items] == ["ok", "ok", "ok"]


def test_each_item_carries_the_exact_data_keys_and_object_id() -> None:
    resp = build_profile_examples(None, frozenset(ResourceKind))
    for item in resp.items:
        # The item object_id is the provider name, and its data carries exactly these keys.
        assert item.object_id in {"local-libvirt", "remote-libvirt", "fault-inject"}
        assert set(cast(dict[str, Any], item.data)) == {
            "provider",
            "profile",
            "note",
            "sizing_note",
            "uses_real_reference",
        }
        assert cast(dict[str, Any], item.data)["provider"] == item.object_id


def test_uses_real_reference_reflects_placeholder_use_with_no_inventory() -> None:
    # With no inventory the local + remote examples fall back to placeholders (uses_real_reference
    # False), while fault-inject owns no rootfs/base image to resolve, so it is never a placeholder
    # (uses_real_reference True). Guards the flag, its `not placeholder` derivation, and the
    # fault-inject placeholder=False constant against inversion.
    examples = _examples(None)
    assert examples["local-libvirt"]["uses_real_reference"] is False
    assert examples["remote-libvirt"]["uses_real_reference"] is False
    assert examples["fault-inject"]["uses_real_reference"] is True


def test_uses_real_reference_tracks_real_vs_placeholder_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # local + remote with real public images -> uses_real_reference True; fault-inject owns no
    # rootfs/base image so it is always a placeholder example (False).
    path = _write_inventory(tmp_path, _FULL_INVENTORY)
    monkeypatch.setenv(SYSTEMS_TOML.name, str(path))
    config.reset()
    doc = load_inventory_optional(path)
    examples = _examples(doc)
    assert examples["local-libvirt"]["uses_real_reference"] is True
    assert examples["remote-libvirt"]["uses_real_reference"] is True
    # fault-inject owns no rootfs/base image, so it is never a placeholder example.
    assert examples["fault-inject"]["uses_real_reference"] is True


def test_local_without_public_image_is_marked_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert examples["local-libvirt"]["uses_real_reference"] is False


def test_only_configured_providers_get_examples(tmp_path: Path) -> None:
    # The caller supplies the composed kinds; build_profile_examples emits exactly those.
    doc = InventoryDoc.parse(
        {
            "schema_version": 2,
            "local_libvirt": [
                {"name": "local-host", "cost_class": "local", "host_uri": "qemu:///system"}
            ],
        }
    )
    examples = _examples(doc, frozenset({_LOCAL}))
    assert set(examples) == {"local-libvirt"}


def test_remote_base_volume_is_placeholder_when_base_image_is_private(tmp_path: Path) -> None:
    # remote-libvirt configured with a base_image that is declared but PRIVATE: the base-volume
    # lookup must return None (a private image's volume must never be surfaced), so the example
    # falls back to the placeholder volume.
    doc = InventoryDoc.parse(
        {
            "schema_version": 2,
            "image": [
                {
                    "provider": "remote-libvirt",
                    "name": "remote-base",
                    "arch": "x86_64",
                    "format": "qcow2",
                    "root_device": "/dev/vda1",
                    "visibility": "private",
                    "source": {"kind": "staged", "volume": "remote-base.qcow2"},
                }
            ],
            "remote_libvirt": [
                {
                    "name": "remote-host",
                    "cost_class": "remote",
                    "uri": "qemu+tls://h/system",
                    "gdb_addr": "10.0.0.5",
                    "gdbstub_range": "1234-1240",
                    "client_cert_ref": "secret://c",  # pragma: allowlist secret
                    "client_key_ref": "secret://k",  # pragma: allowlist secret
                    "ca_cert_ref": "secret://a",  # pragma: allowlist secret
                    "base_image": "remote-base",
                    "vcpus": 4,
                    "memory_mb": 8192,
                }
            ],
        }
    )
    examples = _examples(doc)
    remote = examples["remote-libvirt"]
    assert remote["uses_real_reference"] is False
    volume = _profile_of(remote)["provider"]["remote-libvirt"]["base_image_volume"]
    assert volume == "REPLACE_ME-base-image-volume"
    assert "remote-base.qcow2" not in json.dumps(examples)


def test_remote_base_volume_requires_name_match_and_staged_source(
    tmp_path: Path,
) -> None:
    # A public remote image exists but its name does NOT match the instance's base_image (which is
    # a different, declared image): the lookup must NOT surface that image's volume (guards the
    # `and` joins from widening to `or`).
    doc = InventoryDoc.parse(
        {
            "schema_version": 2,
            "image": [
                {
                    "provider": "remote-libvirt",
                    "name": "some-other-image",
                    "arch": "x86_64",
                    "format": "qcow2",
                    "root_device": "/dev/vda1",
                    "visibility": "public",
                    "source": {"kind": "staged", "volume": "some-other-image.qcow2"},
                },
                {
                    "provider": "remote-libvirt",
                    "name": "remote-base",
                    "arch": "x86_64",
                    "format": "qcow2",
                    "root_device": "/dev/vda1",
                    "visibility": "private",
                    "source": {"kind": "staged", "volume": "remote-base.qcow2"},
                },
            ],
            "remote_libvirt": [
                {
                    "name": "remote-host",
                    "cost_class": "remote",
                    "uri": "qemu+tls://h/system",
                    "gdb_addr": "10.0.0.5",
                    "gdbstub_range": "1234-1240",
                    "client_cert_ref": "secret://c",  # pragma: allowlist secret
                    "client_key_ref": "secret://k",  # pragma: allowlist secret
                    "ca_cert_ref": "secret://a",  # pragma: allowlist secret
                    "base_image": "remote-base",
                    "vcpus": 4,
                    "memory_mb": 8192,
                }
            ],
        }
    )
    examples = _examples(doc)
    remote = examples["remote-libvirt"]
    assert remote["uses_real_reference"] is False
    volume = _profile_of(remote)["provider"]["remote-libvirt"]["base_image_volume"]
    assert volume == "REPLACE_ME-base-image-volume"
    assert "some-other-image" not in json.dumps(examples)


def test_remote_base_volume_requires_staged_source_kind(tmp_path: Path) -> None:
    # The matching PUBLIC remote image exists and its name equals the instance base_image, but its
    # source kind is s3 (not staged) — there is no host volume to provision from. The lookup must
    # return None and fall back to the placeholder. Isolates the `isinstance(..., StagedSource)`
    # conjunct: a mutant dropping it (or widening the `and` to `or`) would surface a real reference.
    doc = InventoryDoc.parse(
        {
            "schema_version": 2,
            "image": [
                {
                    "provider": "remote-libvirt",
                    "name": "remote-base",
                    "arch": "x86_64",
                    "format": "qcow2",
                    "root_device": "/dev/vda1",
                    "visibility": "public",
                    "source": {"kind": "s3", "object_key": "images/remote-base.qcow2"},
                }
            ],
            "remote_libvirt": [
                {
                    "name": "remote-host",
                    "cost_class": "remote",
                    "uri": "qemu+tls://h/system",
                    "gdb_addr": "10.0.0.5",
                    "gdbstub_range": "1234-1240",
                    "client_cert_ref": "secret://c",  # pragma: allowlist secret
                    "client_key_ref": "secret://k",  # pragma: allowlist secret
                    "ca_cert_ref": "secret://a",  # pragma: allowlist secret
                    "base_image": "remote-base",
                    "vcpus": 4,
                    "memory_mb": 8192,
                }
            ],
        }
    )
    examples = _examples(doc)
    remote = examples["remote-libvirt"]
    assert remote["uses_real_reference"] is False
    volume = _profile_of(remote)["provider"]["remote-libvirt"]["base_image_volume"]
    assert volume == "REPLACE_ME-base-image-volume"


# --- ADR-0269: provider set driven by composed kinds ---


def test_examples_cover_exactly_the_composed_kinds() -> None:
    resp = build_profile_examples(None, frozenset({_LOCAL}))
    assert _providers(resp) == {"local-libvirt"}


def test_fault_inject_absent_unless_composed() -> None:
    without = build_profile_examples(None, frozenset({_LOCAL, _REMOTE}))
    assert "fault-inject" not in _providers(without)
    with_fault = build_profile_examples(None, frozenset({_LOCAL, _FAULT}))
    assert "fault-inject" in _providers(with_fault)


def test_empty_composed_set_yields_no_examples() -> None:
    resp = build_profile_examples(None, frozenset())
    assert resp.items == []
