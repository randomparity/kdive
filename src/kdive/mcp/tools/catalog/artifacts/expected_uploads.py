"""``artifacts.expected_uploads`` — discoverable upload-artifact vocabulary (#551, ADR-0166).

A static, read-only, auth-only discovery tool (auth posture per ADR-0117: a valid token
gates the transport as defence-in-depth, but there is no platform/project gate and no
audit). It advertises the accepted ``name`` vocabulary for each upload owner-kind so a
black-box client can learn the set *before* an ``artifacts.create_run_upload`` /
``artifacts.create_system_upload`` attempt, instead of discovering it through a rejection.

The vocabulary is a module constant (the same sets the upload validator enforces), so the
projection can never drift from the accepted names.
"""

from __future__ import annotations

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    CREATE_SYSTEM_UPLOAD_TOOL,
    RUN_ARTIFACT_NAMES,
    SYSTEM_ARTIFACT_NAMES,
)
from kdive.serialization import JsonValue

_OBJECT_ID = "expected-uploads"

# Next tools a caller follows once it knows the vocabulary.
_NEXT_ACTIONS = [CREATE_RUN_UPLOAD_TOOL, CREATE_SYSTEM_UPLOAD_TOOL]

# One-line purpose per accepted artifact name, so a cold agent knows which file maps to
# which name (the issue's core confusion: a boot bzImage must be declared ``kernel``).
_NAME_DESCRIPTIONS: dict[str, str] = {
    "kernel": "Combined kernel+modules tar (gzip): boot/vmlinuz (the bzImage, NOT the vmlinux "
    "ELF) + lib/modules/<release>/, declared as 'kernel'. No separate 'modules' upload. "
    "See resource://kdive/docs/operating/external-build-upload.md for the tar recipe.",
    "vmlinux": "Uncompressed kernel ELF with DWARF debug info, declared as 'vmlinux'.",
    "initrd": "Initial ramdisk / initramfs image.",
    "effective_config": "The kernel .config used for the build (<= 1 MiB).",
    "rootfs": "Root filesystem image for a DEFINED System's upload window.",
}


def _owner_item(owner_kind: str, accepted: frozenset[str], create_tool: str) -> ToolResponse:
    """Build one discovery item for an upload owner-kind."""
    names = sorted(accepted)
    accepted_names: list[JsonValue] = list(names)
    descriptions: dict[str, JsonValue] = {name: _NAME_DESCRIPTIONS[name] for name in names}
    data: dict[str, JsonValue] = {
        "owner_kind": owner_kind,
        "accepted_names": accepted_names,
        "create_tool": create_tool,
        "descriptions": descriptions,
    }
    return ToolResponse.success(owner_kind, "ok", data=data)


def expected_uploads() -> ToolResponse:
    """Return the accepted upload-artifact vocabulary per owner-kind.

    Returns:
        A :class:`ToolResponse` collection with one item per upload owner-kind (``run``,
        ``system``); each item's ``data`` carries ``owner_kind``, ``accepted_names``
        (sorted), the literal ``create_tool`` name, and a per-name ``descriptions`` map.
    """
    items = [
        _owner_item("run", RUN_ARTIFACT_NAMES, CREATE_RUN_UPLOAD_TOOL),
        _owner_item("system", SYSTEM_ARTIFACT_NAMES, CREATE_SYSTEM_UPLOAD_TOOL),
    ]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )
