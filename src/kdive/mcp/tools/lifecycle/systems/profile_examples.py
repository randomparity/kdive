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

The local-libvirt example also carries the profile's ``debug`` block (``gdbstub``/
``preserve_on_crash``, both non-sensitive booleans defaulting off) so an agent learns these
provision-bound knobs exist from the example shape itself, not just from the guides (#1014,
BLACK_BOX_REVIEW.md Finding 3(a)).
"""

from __future__ import annotations

from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.catalog.resources import ResourceKind
from kdive.inventory.model import ImageEntry, InventoryDoc, StagedSource
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provider_sections import PROVIDER_SECTIONS
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
# disk-image example omits it entirely. kernel_source_ref is an inert provenance annotation with
# no runtime reader and no valid-value set to discover — any non-empty string is accepted, so the
# example value below is illustrative, not something the caller must look up or match. It is kept
# non-URI-looking anyway, only so it isn't mistaken for the unrelated runs.create structured
# {"git": {...}} build source, which has real dispatch semantics.
_PLACEHOLDER_KERNEL_SOURCE = "example-baseline-label"

_REPLACE_NOTE = (
    "Example shape only; replace every REPLACE_ME placeholder (any rootfs / base_image_volume "
    "reference) with a real value for your host before provisioning. kernel_source_ref is an "
    "arbitrary provenance label you choose (any non-empty string) for the baseline kernel — there "
    "is no valid-value set to discover or match against, so the example value can be kept as-is or "
    'replaced with any label meaningful to you. It is unrelated to the structured {"git": '
    '{"remote": ..., "ref": ...}} build source at runs.create. The disk-image example needs no '
    "kernel_source_ref: it boots the operator-staged base image's own kernel. The local-libvirt "
    "example's provider.local-libvirt.debug block (gdbstub/preserve_on_crash) is bound at "
    "systems.provision: set it here if you intend to debug or triage this System, since it cannot "
    "be added to a System that is already provisioned without reprovisioning. The provider "
    "destructive_ops list opts into force_crash (deliberate kernel crash / fault injection) "
    "only; leave it empty unless you need that. control.power (reboot/off/cycle/reset) and "
    "systems.reprovision are contributor lifecycle and do not require it."
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


def build_profile_examples(
    doc: InventoryDoc | None, kinds: frozenset[ResourceKind]
) -> ToolResponse:
    """Build the example-profiles collection for the deployment's composed providers.

    Args:
        doc: The parsed ``systems.toml`` inventory, or ``None`` when no file is present.
        kinds: The providers composed in this deployment (``resolver.registered_kinds()``);
            one example is emitted per composed kind, ordered by ``ResourceKind``.

    Returns:
        A :class:`ToolResponse` collection with one item per kind in ``kinds``; each item's
        ``data`` carries ``provider``, the ready-to-edit ``profile`` dict, and (when a placeholder
        is used) a ``note``.
    """
    providers = [PROVIDER_SECTIONS[kind].alias for kind in ResourceKind if kind in kinds]
    items = [_example_item(provider, doc) for provider in providers]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )


def _example_item(provider: str, doc: InventoryDoc | None) -> ToolResponse:
    """Build one example item for ``provider`` from the inventory (or placeholders).

    Every example carries a ``note``: a direct-kernel example carries an illustrative
    ``kernel_source_ref`` value (an inert provenance label with no valid-value set — the caller may
    keep it or replace it with any label meaningful to them), while the disk-image (remote-libvirt)
    example omits it entirely (it boots the base image's own kernel, #472). Even when the
    rootfs/base-image reference is resolved to a real inventory name, the direct-kernel source
    stays the illustrative value.
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
    if provider == _LOCAL:
        # Disclose that the example image was picked by declaration order and point the agent to
        # images.list to choose deliberately (#1017): the vacuum that made agents reuse this one.
        data.update(_local_selection_context(doc))
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

    Carries an explicit ``debug`` block (``gdbstub``/``preserve_on_crash``, both off) so an agent
    reading this example learns the knobs exist without having to find them elsewhere;
    ``_REPLACE_NOTE`` tells the caller they are provision-time-only (#1014, BLACK_BOX_REVIEW.md
    Finding 3(a)). Neither ``remote-libvirt`` nor ``fault-inject`` has a ``debug`` field
    (``RemoteLibvirtProfile``'s gdbstub is unconditional; ``FaultInjectProfile`` owns no
    crash-capture flags), so only this example carries the block.
    """
    image = _public_image(doc, _LOCAL)
    rootfs: JsonValue
    if image is not None:
        rootfs = {"kind": "catalog", "provider": _LOCAL, "name": image.name}
        placeholder = False
    else:
        rootfs = {"kind": "local", "path": _PLACEHOLDER_ROOTFS_PATH}
        placeholder = True
    debug: JsonValue = {"gdbstub": False, "preserve_on_crash": False}
    provider: JsonValue = {_LOCAL: {"rootfs": rootfs, "debug": debug}}
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


def _public_images(doc: InventoryDoc | None, provider: str) -> list[ImageEntry]:
    """Every ``PUBLIC``-visibility ``[[image]]`` declared for ``provider``, in declaration order."""
    if doc is None:
        return []
    return [
        image
        for image in doc.image
        if image.provider == provider and image.visibility == ImageVisibility.PUBLIC
    ]


def _public_image(doc: InventoryDoc | None, provider: str) -> ImageEntry | None:
    """The first ``PUBLIC``-visibility ``[[image]]`` declared for ``provider``, or ``None``."""
    images = _public_images(doc, provider)
    return images[0] if images else None


_KERNEL_TRAP_NOTE = (
    "This example's boot_method is direct-kernel with no baseline_kernel set, but the chosen image "
    "may be direct_kernel: not_provisionable (2+ kernels in /boot, fail-closed at provision). Call "
    "images.describe on it and check capability_signals.direct_kernel first: if it reads "
    "not_provisionable, add a provider.local-libvirt.baseline_kernel hint naming one of its "
    "candidates, or pick a provisionable image instead."
)

_SELECTION_NOTE_MANY = (
    "Chosen by declaration order (the first-declared public local-libvirt image); it is one of "
    "{count} public images. Call images.list / images.describe to choose deliberately by "
    "capabilities, os, and description. " + _KERNEL_TRAP_NOTE
)
_SELECTION_NOTE_ONE = "The only public local-libvirt image in this inventory. " + _KERNEL_TRAP_NOTE
_SELECTION_NOTE_NONE = (
    "No public local-libvirt image is declared; this example uses a placeholder rootfs. Declare an "
    "[[image]] (or replace the rootfs path) before provisioning."
)


def _selection_note(count: int) -> str:
    """The count-conditioned disclosure so the note never asserts a choice that does not exist."""
    if count > 1:
        return _SELECTION_NOTE_MANY.format(count=count)
    if count == 1:
        return _SELECTION_NOTE_ONE
    return _SELECTION_NOTE_NONE


def _local_selection_context(doc: InventoryDoc | None) -> dict[str, JsonValue]:
    """The local example's selection disclosure: how many images exist, why this one, its context.

    ``available_images`` lets the agent know a choice exists; ``selection_note`` discloses that the
    example image was picked by declaration order and — when there is a choice — points to
    ``images.list``; ``description`` echoes the chosen image's operator-attested hint (ADR-0311).
    """
    images = _public_images(doc, _LOCAL)
    return {
        "available_images": len(images),
        "selection_note": _selection_note(len(images)),
        "description": images[0].description if images else "",
    }


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
    from kdive.inventory.loader import load_inventory_optional
    from kdive.inventory.path import systems_toml_path

    return load_inventory_optional(systems_toml_path())
