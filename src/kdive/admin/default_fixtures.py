"""Default fixture files installed by ``python -m kdive install-fixtures``.

Image/rootfs definitions are no longer embedded here (ADR-0112): they live only in
``systems.toml`` and load into ``image_catalog`` via the inventory reconcile. The installed
fixture bundle now carries only the **profiles** half — the kernel-config/cmdline policy the
local-libvirt provider checks a built kernel against — plus a manifest that declares an empty
rootfs list (the rootfs catalog is the DB now).

The manifest is built from the :class:`~kdive.components.catalog.FixtureManifest` model
rather than an embedded YAML literal, so this module holds no inline inventory YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kdive.components.catalog import FixtureManifest, FixtureStorage

_PROFILE_RELATIVE = "profiles/console-ready_x86_64.yaml"

_PROFILE_YAML = """provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
requires:
  config:
    required:
      CONFIG_SERIAL_8250_CONSOLE: y
      CONFIG_VIRTIO_BLK: y
      CONFIG_VIRTIO_PCI: y
  cmdline:
    required_tokens:
      - console=ttyS0
      - root=/dev/vda
    protected_prefixes:
      - console=
      - root=
      - crashkernel=
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities:
      - agent
"""


def _manifest_yaml() -> str:
    """Serialize the local-libvirt fixture manifest (empty rootfs list; profiles only)."""
    manifest = FixtureManifest(
        schema_version=1,
        provider="local-libvirt",
        storage=FixtureStorage(
            allowed_component_roots=[Path("/var/lib/kdive/rootfs")],
            cache_dir=Path("/var/lib/kdive/rootfs/cache"),
            overlay_dir=Path("/var/lib/kdive/rootfs/overlays"),
        ),
        rootfs=[],
        profiles=[_PROFILE_RELATIVE],
    )
    return yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False)


def _build_fixture_files() -> dict[str, str]:
    return {
        "manifest.yaml": _manifest_yaml(),
        _PROFILE_RELATIVE: _PROFILE_YAML,
    }


LOCAL_LIBVIRT_FIXTURES: dict[str, str] = _build_fixture_files()
