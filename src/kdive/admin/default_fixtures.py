"""Default fixture files installed by ``python -m kdive install-fixtures``.

Image/rootfs definitions are no longer embedded here (ADR-0112): they live only in
``systems.toml`` and load into ``image_catalog`` via the inventory reconcile. The installed
fixture bundle now carries only the **profiles** half — the kernel-config/cmdline policy the
local-libvirt provider checks a built kernel against — plus a manifest that declares an empty
rootfs list (the rootfs catalog is the DB now).

The manifest is built from the :class:`~kdive.components.catalog.FixtureManifest` model
rather than an embedded YAML literal, so this module holds no inline inventory YAML.

kdive no longer inspects a kernel .config (ADR-0316), so a profile carries no kernel-config/cmdline
requirements — only its ``(provider, name, arch)`` triple (ADR-0319, #1055).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kdive.components.catalog import FixtureManifest, FixtureStorage

_PROFILE_RELATIVE = "profiles/console-ready_x86_64.yaml"

_PROFILE_YAML = """provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
"""

# The ppc64le sibling (#1144, epic #1139). Same shape as the x86_64 profile — just the
# (provider, name, arch) triple; arch=ppc64le is what routes a System pointed at it through the
# pseries arch traits (machine=pseries, console=hvc0; kdive.domain.platform).
_PPC64LE_PROFILE_RELATIVE = "profiles/console-ready_ppc64le.yaml"

_PPC64LE_PROFILE_YAML = """provider: local-libvirt
name: console-ready_ppc64le
arch: ppc64le
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
        profiles=[_PROFILE_RELATIVE, _PPC64LE_PROFILE_RELATIVE],
    )
    return yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False)


def _build_fixture_files() -> dict[str, str]:
    return {
        "manifest.yaml": _manifest_yaml(),
        _PROFILE_RELATIVE: _PROFILE_YAML,
        _PPC64LE_PROFILE_RELATIVE: _PPC64LE_PROFILE_YAML,
    }


LOCAL_LIBVIRT_FIXTURES: dict[str, str] = _build_fixture_files()
