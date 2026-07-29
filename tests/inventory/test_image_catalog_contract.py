"""Contract test: the ansible facts template emits a systems.toml the app accepts.

Renders the real ``systems_toml_block.j2`` (the ansible -> app seam, #598/ADR-0188)
with a representative four-image host context and asserts ``InventoryDoc.parse``
accepts it: image identities unique, ``base_image`` resolves, every source is
``staged``. A template typo (wrong field, missing ``[image.source]``) makes this fail.

Since ADR-0481 (#1629) the template declares a ``staged`` source only for volumes the
role confirmed present in the host's pool, so the context carries a confirmed and an
unconfirmed selection rather than one flat list. The unconfirmed cases below pin the
regression this issue names: an image the host never built must not register a catalog
row that provisioning can only fail on.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import jinja2
import pytest

from kdive.inventory.model import InventoryDoc, InventoryError

_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2"
)

_DEFAULTS = {
    "packages": ["qemu-guest-agent"],
    "helpers": ["kdive-install-kernel"],
    "include_kernel_debuginfo": False,
    "crashkernel": "256M",
    "arches": ["x86_64"],
    "root_device": "/dev/vda",
    "arch_alias": {"x86_64": "amd64", "aarch64": "arm64", "ppc64le": "ppc64el"},
}

# A four-image selection (fedora/ubuntu/rocky/bare) as the role would resolve it.
_FEDORA = {"name": "fedora-kdive-remote-base-43", "distro": "fedora", "source": "virt-builder"}
_UBUNTU = {"name": "ubuntu-2404-kdive-remote-base", "distro": "ubuntu", "source": "cloud-image"}
_ROCKY = {"name": "rocky-10-kdive-remote-base", "distro": "rocky", "source": "cloud-image"}
_BARE = {
    "name": "bare-kdive-remote-base",
    "distro": "bare",
    "source": "scratch",
    "root_device": "/dev/vda1",
}
_SELECTED = [_FEDORA, _UBUNTU, _ROCKY, _BARE]

_CONTEXT = {
    "kdive_image_defaults": _DEFAULTS,
    "remote_libvirt_facts_staged": _SELECTED,
    "remote_libvirt_facts_missing": [],
    "kdive_default_image": "fedora-kdive-remote-base-43",
    "storage_pool_target": "/var/lib/libvirt/images",
    "ansible_architecture": "x86_64",
    "inventory_hostname": "host-a",
    "remote_host_fqdn": "host-a.example.test",
    "gdb_addr": "192.168.12.2",
    "gdbstub_range": "47000:47099",
    "remote_libvirt_facts_client_cert_ref": "clientcert.pem",
    "remote_libvirt_facts_client_key_ref": "clientkey.pem",  # pragma: allowlist secret
    "remote_libvirt_facts_ca_cert_ref": "cacert.pem",
    "cost_class": "remote",
    "concurrent_allocation_cap": 1,
    "vcpus": 16,
    "memory_mb": 65536,
    "shapes": ["small", "medium", "large", "max"],
    "machine_type": {"x86_64": "pc", "ppc64le": "pseries"},
}


def _render(**overrides: Any) -> str:
    text = _TEMPLATE.read_text(encoding="utf-8")
    template = jinja2.Template(text, undefined=jinja2.StrictUndefined)
    return template.render(**{**_CONTEXT, **overrides})


def _parsed(**overrides: Any) -> InventoryDoc:
    # The template emits the paste-in fragment ([[image]] + [[remote_libvirt]]) with no
    # schema_version (that lives at the top of the deployment's systems.toml). Compose a
    # full v2 doc, decode the TOML, and hand the dict to InventoryDoc.parse — which takes
    # a decoded mapping, not a string, and requires schema_version=2.
    data = tomllib.loads("schema_version = 2\n" + _render(**overrides))
    return InventoryDoc.parse(data)


def test_template_emits_one_image_block_per_confirmed_image() -> None:
    doc = _parsed()
    names = sorted(img.name for img in doc.image)
    assert names == sorted(i["name"] for i in _SELECTED)


def test_template_image_identities_unique_and_staged() -> None:
    doc = _parsed()
    for img in doc.image:
        assert img.provider == "remote-libvirt"
        assert img.arch == "x86_64"
        assert img.source.kind == "staged"
        assert img.source.volume == f"{img.name}.qcow2"


def test_template_default_base_image_resolves() -> None:
    doc = _parsed()
    declared = {img.name for img in doc.image}
    assert doc.remote_libvirt[0].base_image in declared
    assert doc.remote_libvirt[0].base_image == "fedora-kdive-remote-base-43"


def test_unconfirmed_image_is_never_declared_staged() -> None:
    # The #1629 regression: bare-kdive-remote-base is selected on this host but its build
    # was skipped, so no volume exists. Declaring it would register a catalog row that
    # provisioning can only fail on.
    overrides = {
        "remote_libvirt_facts_staged": [_FEDORA, _ROCKY],
        "remote_libvirt_facts_missing": [_BARE],
    }
    declared = {img.name for img in _parsed(**overrides).image}
    assert declared == {"fedora-kdive-remote-base-43", "rocky-10-kdive-remote-base"}
    # Nothing anywhere in the fragment may claim the volume, commented-out lines included.
    assert 'volume = "bare-kdive-remote-base.qcow2"' not in _render(**overrides)


def test_unconfirmed_image_is_recorded_as_an_omission() -> None:
    # Omitting it silently would be its own trap: the operator needs to see that the host
    # is short an image and what to run.
    rendered = _render(remote_libvirt_facts_staged=[_FEDORA], remote_libvirt_facts_missing=[_BARE])
    omission = rendered.split("\n[[image]]", 1)[0]
    assert "OMITTED" in omission
    assert "bare-kdive-remote-base" in omission
    assert "playbooks/image.yml" in omission


def test_unconfirmed_default_image_is_rejected_at_load() -> None:
    # base_image naming an unstaged image now fails closed at InventoryDoc.parse rather
    # than passing validation and failing later, at provision time, on a real System.
    with pytest.raises(InventoryError, match="names undeclared image"):
        _parsed(
            remote_libvirt_facts_staged=[_ROCKY],
            remote_libvirt_facts_missing=[_FEDORA],
            kdive_default_image="fedora-kdive-remote-base-43",
        )


def test_incomplete_fragment_says_why_it_will_not_load() -> None:
    rendered = _render(
        remote_libvirt_facts_staged=[_ROCKY],
        remote_libvirt_facts_missing=[_FEDORA],
        kdive_default_image="fedora-kdive-remote-base-43",
    )
    assert "INCOMPLETE" in rendered


def test_complete_fragment_carries_no_incompleteness_warning() -> None:
    assert "OMITTED" not in _render()
    assert "INCOMPLETE" not in _render()
