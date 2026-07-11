"""``artifacts.expected_uploads`` — discoverable upload-artifact contract (#551, #769, ADR-0166).

A static, read-only, auth-only discovery tool (auth posture per ADR-0117: a valid token gates the
transport as defence-in-depth, but there is no platform/project gate and no audit). It advertises,
per upload owner-kind, the accepted ``name`` vocabulary and — for the run build artifacts — the full
byte contract an external builder must produce: required-vs-optional, the format/magic, and the
internal tar layout (ADR-0234 §5). An agent can learn exactly what bytes to upload from MCP alone,
without reading source or triggering a rejection.

The run contracts come from :data:`kdive.build_artifacts.validation.EXTERNAL_BUILD_CONTRACTS`, whose
magic/layout/cap fields are derived from the validator's own constants, so the advertisement cannot
drift from what ``validate_external_artifacts`` enforces.
"""

from __future__ import annotations

from collections.abc import Mapping

from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.build_artifacts.validation import (
    EXTERNAL_BUILD_CONTRACTS,
    ArtifactContract,
    FormatContract,
)
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    FEATURE_CONFIG_REQUIREMENTS_TOOL,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    CREATE_SYSTEM_UPLOAD_TOOL,
)
from kdive.serialization import JsonValue

_OBJECT_ID = "expected-uploads"

# The literal tool name, shared so callers that chain into the upload loop (runs.create external,
# runs.complete_build validation failure) point at the same name this tool registers under.
EXPECTED_UPLOADS_TOOL = "artifacts.expected_uploads"

# The human recipe for the combined kernel tar, surfaced so an agent can follow it after learning
# the contract. Kept as a literal resource URI (not a markdown link) — it is a pointer, not a file
# reference resolved by the docs-link guard.
EXTERNAL_BUILD_UPLOAD_DOC = "resource://kdive/docs/operating/external-build-upload.md"

# Next tools a caller follows once it knows the vocabulary: learn which CONFIG_* the debug
# features need (ADR-0318), then upload.
_NEXT_ACTIONS = [
    FEATURE_CONFIG_REQUIREMENTS_TOOL,
    CREATE_RUN_UPLOAD_TOOL,
    CREATE_SYSTEM_UPLOAD_TOOL,
]

# The system (rootfs) upload contract. rootfs is not the combined-tar validator's concern and
# enforces no magic at this seam, so its contract is minimal — but it is surfaced in the same
# structure so a black-box client reads one shape across owner kinds.
_SYSTEM_CONTRACTS: Mapping[str, ArtifactContract] = {
    "rootfs": ArtifactContract(
        name="rootfs",
        requirement="required",
        summary="Root filesystem image for a DEFINED System's upload window.",
        format=FormatContract(container="filesystem image"),
    ),
}


def _owner_item(
    owner_kind: str,
    accepted: frozenset[str],
    create_tool: str,
    contracts: Mapping[str, ArtifactContract],
    *,
    provider_neutral: bool,
    doc: str | None,
) -> ToolResponse:
    """Build one discovery item for an upload owner-kind.

    ``contracts`` must hold an entry for every accepted name; a missing entry raises ``KeyError``
    here (a loud build-time failure) rather than silently advertising an artifact with no contract.
    """
    names = sorted(accepted)
    accepted_names: list[JsonValue] = list(names)
    contract_json: dict[str, JsonValue] = {name: contracts[name].to_json() for name in names}
    data: dict[str, JsonValue] = {
        "owner_kind": owner_kind,
        "accepted_names": accepted_names,
        "create_tool": create_tool,
        "contracts": contract_json,
    }
    if provider_neutral:
        data["provider_neutral"] = True
    if doc is not None:
        data["doc"] = doc
    return ToolResponse.success(owner_kind, "ok", data=data)


def expected_uploads() -> ToolResponse:
    """Return the accepted upload-artifact contract per owner-kind.

    Returns:
        A :class:`ToolResponse` collection with one item per upload owner-kind (``run``,
        ``system``); each item's ``data`` carries ``owner_kind``, ``accepted_names`` (sorted), the
        literal ``create_tool`` name, and a per-name ``contracts`` map. The ``run`` item also adds
        ``provider_neutral`` (the format is one shape across providers) and ``doc`` (the recipe).
    """
    items = [
        _owner_item(
            "run",
            RUN_ARTIFACT_NAMES,
            CREATE_RUN_UPLOAD_TOOL,
            EXTERNAL_BUILD_CONTRACTS,
            provider_neutral=True,
            doc=EXTERNAL_BUILD_UPLOAD_DOC,
        ),
        _owner_item(
            "system",
            SYSTEM_ARTIFACT_NAMES,
            CREATE_SYSTEM_UPLOAD_TOOL,
            _SYSTEM_CONTRACTS,
            provider_neutral=False,
            doc=None,
        ),
    ]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )
