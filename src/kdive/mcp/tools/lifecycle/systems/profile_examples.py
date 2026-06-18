"""``systems.profile_examples`` — discoverable example provisioning profiles (#451, ADR-0124).

A read-only, auth-only discovery tool (modeled on ``projects.list``, ADR-0117: a valid token is
required as defence-in-depth, but there is no platform/project gate and no audit). It projects the
``systems.toml`` inventory into one ready-to-edit example profile per **configured** provider, so a
new agent can learn a valid profile shape from the MCP surface alone rather than guessing.

Data contract (this tool is *not* ``projects.list``, which returns the caller's own token claims —
this projects the shared inventory). It reads **only** non-sensitive inventory identifiers: the
provider name and a ``PUBLIC``-visibility ``[[image]]`` name (for remote, the instance's
``base_image``, itself a declared image). It never reads or emits the ``[[remote_libvirt]]``
``uri``/``gdb_addr``/``gdbstub_range`` or any ``*_cert_ref`` secret-ref name, and it excludes
``private``-visibility images — so no operator-private or other-tenant identifier can reach the
wire. The examples are schema-and-policy valid as emitted (they parse and pass provider policy), but
not necessarily provisionable as-is: a placeholder reference must be replaced with a real one for
the caller's host.
"""

from __future__ import annotations

from pathlib import Path

from kdive.domain.catalog.images import ImageVisibility
from kdive.inventory.model import ImageEntry, InventoryDoc, StagedSource
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue

_OBJECT_ID = "profile-examples"

# The discovery→provision lifecycle a cold agent should follow (#474). Each is a registered tool
# identifier; the order walks `resources.list` (resource kind/id) → `shapes.list` (sizing) →
# `accounting.estimate` (cost) so the `allocations.request` is built from discovered context and
# granted on the first valid attempt, then provisions and tears down. `systems.define` (the
# two-step define-then-provision lane) stays directly callable but is not led to from here.
_NEXT_ACTIONS = [
    "resources.list",
    "shapes.list",
    "accounting.estimate",
    "allocations.request",
    "systems.provision",
    "systems.get",
    "systems.teardown",
    "allocations.release",
]

# The three provider sections an example targets, keyed by the alias the profile schema uses.
_LOCAL = "local-libvirt"
_REMOTE = "remote-libvirt"
_FAULT = "fault-inject"

# Placeholder references the caller must replace; absolute path so a `local` rootfs ref parses.
_PLACEHOLDER_ROOTFS_PATH = "/REPLACE_ME/rootfs.img"
_PLACEHOLDER_BASE_IMAGE = "REPLACE_ME-base-image-volume"

# Placeholder kernel source for the direct-kernel (build-iterating) lane only. A disk-image
# provision boots the base image's own kernel and never reads kernel_source_ref (#472), so the
# disk-image example omits it entirely.
_PLACEHOLDER_KERNEL_SOURCE = "git:REPLACE_ME-kernel-source"

_REPLACE_NOTE = (
    "Example shape only; replace every REPLACE_ME placeholder (any rootfs / base_image_volume "
    "reference, and kernel_source_ref on the direct-kernel examples) with a real value for your "
    "host before provisioning. The disk-image example needs no kernel_source_ref: it boots the "
    "operator-staged base image's own kernel."
)

# Sizing guidance (#461): the example carries concrete vcpu/memory_mb/disk_gb so it parses alone
# and provisions a full-custom (no-shape) allocation as-is. But a shape-sized allocation resolves
# its own vcpu/memory_mb/disk_gb, and `systems.provision` rejects a profile that restates a
# *different* size (`reconcile_profile_sizing`). So an agent provisioning onto a shape-sized
# allocation must omit these three fields (they are filled from the allocation) or match the shape.
_SIZING_NOTE = (
    "vcpu/memory_mb/disk_gb are an example custom size: when provisioning onto a shape-sized "
    "allocation, omit these three fields (they are filled from the allocation) or set them to the "
    "shape's size — a mismatch is rejected as a configuration_error."
)

# The provider-agnostic core every example carries; sizing is concrete so the example parses alone.
# kernel_source_ref is NOT here: it is required only on the direct-kernel lane (#472), so the
# direct-kernel builders add it and the disk-image (remote-libvirt) example omits it.
_CORE: dict[str, JsonValue] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
}


def build_profile_examples(doc: InventoryDoc | None) -> ToolResponse:
    """Build the example-profiles collection from an inventory document (or ``None``).

    Args:
        doc: The parsed ``systems.toml`` inventory, or ``None`` when no file is present (the
            gitignored pre-config state). With ``None``, or a doc that configures no provider
            instance, the default placeholder set (one example per provider kind) is returned.

    Returns:
        A :class:`ToolResponse` collection with one item per configured provider; each item's
        ``data`` carries ``provider``, the ready-to-edit ``profile`` dict, and (when a placeholder
        is used) a ``note``.
    """
    providers = _configured_providers(doc)
    items = [_example_item(provider, doc) for provider in providers]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )


def _configured_providers(doc: InventoryDoc | None) -> list[str]:
    """The providers to emit an example for: those with a declared instance, else all three."""
    if doc is None:
        return [_LOCAL, _REMOTE, _FAULT]
    configured = []
    if doc.local_libvirt:
        configured.append(_LOCAL)
    if doc.remote_libvirt:
        configured.append(_REMOTE)
    if doc.fault_inject:
        configured.append(_FAULT)
    return configured or [_LOCAL, _REMOTE, _FAULT]


def _example_item(provider: str, doc: InventoryDoc | None) -> ToolResponse:
    """Build one example item for ``provider`` from the inventory (or placeholders).

    Every example carries a ``note``: a direct-kernel example carries a placeholder
    ``kernel_source_ref`` the caller must replace, while the disk-image (remote-libvirt) example
    omits it entirely (it boots the base image's own kernel, #472). Even when the rootfs/base-image
    reference is resolved to a real inventory name, the direct-kernel source stays a placeholder.
    It also carries a ``sizing_note`` (#461) telling the caller the example's concrete
    ``vcpu``/``memory_mb``/``disk_gb`` must be omitted or matched when provisioning onto a
    shape-sized allocation. ``uses_real_reference`` reports whether the provider rootfs/base-image
    was a real inventory ref.
    """
    profile, placeholder = _example_profile(provider, doc)
    data: dict[str, JsonValue] = {
        "provider": provider,
        "profile": profile,
        "note": _REPLACE_NOTE,
        "sizing_note": _SIZING_NOTE,
        "uses_real_reference": not placeholder,
    }
    return ToolResponse.success(provider, "ok", data=data)


def _example_profile(provider: str, doc: InventoryDoc | None) -> tuple[dict[str, JsonValue], bool]:
    """Return ``(profile, used_placeholder)`` for ``provider``."""
    if provider == _REMOTE:
        return _remote_profile(doc)
    if provider == _FAULT:
        return _fault_profile(), False
    return _local_profile(doc)


def _local_profile(doc: InventoryDoc | None) -> tuple[dict[str, JsonValue], bool]:
    """A ``local-libvirt`` example: a ``catalog`` rootfs when a public image exists, else ``local``.

    A placeholder ``catalog`` name would fail ``validate_rootfs_reference`` when an inventory file
    is present (an undeclared catalog name raises), so the fallback uses a ``local`` rootfs (which
    is not inventory-checked) to keep every emitted example policy-valid.
    """
    image = _public_image(doc, _LOCAL)
    rootfs: JsonValue
    if image is not None:
        rootfs = {"kind": "catalog", "provider": _LOCAL, "name": image.name}
        placeholder = False
    else:
        rootfs = {"kind": "local", "path": _PLACEHOLDER_ROOTFS_PATH}
        placeholder = True
    provider: JsonValue = {_LOCAL: {"rootfs": rootfs}}
    profile: dict[str, JsonValue] = {
        **_CORE,
        "boot_method": "direct-kernel",
        "kernel_source_ref": _PLACEHOLDER_KERNEL_SOURCE,
        "provider": provider,
    }
    return profile, placeholder


def _remote_profile(doc: InventoryDoc | None) -> tuple[dict[str, JsonValue], bool]:
    """A ``remote-libvirt`` example: ``disk-image`` boot + a ``base_image_volume``."""
    staged_volume = _remote_base_volume(doc)
    placeholder = staged_volume is None
    volume = staged_volume if staged_volume is not None else _PLACEHOLDER_BASE_IMAGE
    provider: JsonValue = {_REMOTE: {"base_image_volume": volume}}
    profile: dict[str, JsonValue] = {
        **_CORE,
        "boot_method": "disk-image",
        "provider": provider,
    }
    return profile, placeholder


def _fault_profile() -> dict[str, JsonValue]:
    """A ``fault-inject`` example: no rootfs (the section owns none)."""
    provider: JsonValue = {_FAULT: {}}
    return {
        **_CORE,
        "boot_method": "direct-kernel",
        "kernel_source_ref": _PLACEHOLDER_KERNEL_SOURCE,
        "provider": provider,
    }


def _public_image(doc: InventoryDoc | None, provider: str) -> ImageEntry | None:
    """The first ``PUBLIC``-visibility ``[[image]]`` declared for ``provider``, or ``None``."""
    if doc is None:
        return None
    for image in doc.image:
        if image.provider == provider and image.visibility == ImageVisibility.PUBLIC:
            return image
    return None


def _remote_base_volume(doc: InventoryDoc | None) -> str | None:
    """The operator-staged libvirt **volume** for the remote instance's base image, else ``None``.

    ``base_image_volume`` is the staged volume name the provider looks up on the host's storage
    pool (``rootfs_build.py``, ADR-0080) — not the catalog image name. The instance's
    ``base_image`` cross-references a declared ``[[image]]`` (the loader enforces this); this
    returns that image's ``staged`` source volume, and only when the declared image is ``PUBLIC``
    (a private image's volume must not be surfaced) and is actually staged (a non-staged source has
    no host volume to provision from).
    """
    if doc is None or not doc.remote_libvirt:
        return None
    base_image = doc.remote_libvirt[0].base_image
    public = _public_image(doc, _REMOTE)
    if public is not None and public.name == base_image and isinstance(public.source, StagedSource):
        return public.source.volume
    return None


def load_inventory_for_examples() -> InventoryDoc | None:
    """Load the configured ``systems.toml`` for the examples tool (``None`` when absent)."""
    import kdive.config as config
    from kdive.config.core_settings import SYSTEMS_TOML
    from kdive.inventory.loader import load_inventory_optional

    raw = config.get(SYSTEMS_TOML) or "./systems.toml"
    return load_inventory_optional(Path(raw))
