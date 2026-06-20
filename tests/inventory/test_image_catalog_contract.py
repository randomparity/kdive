"""Contract test: the ansible facts template emits a systems.toml the app accepts.

Renders the real ``systems_toml_block.j2`` (the ansible -> app seam, #598/ADR-0188)
with a representative four-image host context and asserts ``InventoryDoc.parse``
accepts it: image identities unique, ``base_image`` resolves, every source is
``staged``. A template typo (wrong field, missing ``[image.source]``) makes this fail.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import jinja2

from kdive.inventory.model import InventoryDoc

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
_SELECTED = [
    {"name": "fedora-kdive-remote-base-43", "distro": "fedora", "source": "virt-builder"},
    {"name": "ubuntu-2404-kdive-remote-base", "distro": "ubuntu", "source": "cloud-image"},
    {"name": "rocky-10-kdive-remote-base", "distro": "rocky", "source": "cloud-image"},
    {
        "name": "bare-kdive-remote-base",
        "distro": "bare",
        "source": "scratch",
        "root_device": "/dev/vda1",
    },
]

_CONTEXT = {
    "kdive_image_defaults": _DEFAULTS,
    "kdive_selected_images": _SELECTED,
    "kdive_default_image": "fedora-kdive-remote-base-43",
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


def _parsed() -> InventoryDoc:
    # The template emits the paste-in fragment ([[image]] + [[remote_libvirt]]) with no
    # schema_version (that lives at the top of the deployment's systems.toml). Compose a
    # full v2 doc, decode the TOML, and hand the dict to InventoryDoc.parse — which takes
    # a decoded mapping, not a string, and requires schema_version=2.
    text = _TEMPLATE.read_text(encoding="utf-8")
    rendered = jinja2.Template(text, undefined=jinja2.StrictUndefined).render(**_CONTEXT)
    data = tomllib.loads("schema_version = 2\n" + rendered)
    return InventoryDoc.parse(data)


def test_template_emits_one_image_block_per_selected_image() -> None:
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
