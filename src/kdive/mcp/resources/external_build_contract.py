"""Generated external-build requirement contract resource (#1579).

The resource replaces the former static artifact discovery tools with one JSON document. Its
payload is derived from the same validator and kernel-config source functions that enforce or
advise the upload lane, so the served resource and runtime behavior cannot drift silently.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.build_artifacts.validation import (
    EXTERNAL_BUILD_CONTRACTS,
    ArtifactContract,
    FormatContract,
)
from kdive.kernel_config.requirements import enforcement_legend, feature_manifest
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_INVESTIGATION_UPLOAD_TOOL,
    CREATE_RUN_UPLOAD_TOOL,
)
from kdive.serialization import JsonValue

EXTERNAL_BUILD_CONTRACT_URI = "resource://kdive/contracts/external-build"
EXTERNAL_BUILD_UPLOAD_DOC = "resource://kdive/docs/operating/external-build-upload.md"

# 3 since #1867 (ADR-0546 §4): a `feature_config_requirements` entry's `gated` bool is replaced by
# `enforcement`, which names where an omission surfaces, and a refusing entry publishes the clauses
# it refuses on as `refuses_on`. Removing a key every entry carried is the case ADR-0544 §6 left
# open when it bumped to 2 for the clause-object element and recorded that an *added* optional key
# does not bump again - `built_in` (#1860) and `arch` (#1859) arrived under that rule.
_SCHEMA_VERSION = 3

_INVESTIGATION_CONTRACTS: Mapping[str, ArtifactContract] = {
    "rootfs": ArtifactContract(
        name="rootfs",
        requirement="required",
        summary=(
            "Root filesystem image for an Investigation's upload window "
            "(reusable across its bound Systems)."
        ),
        format=FormatContract(container="filesystem image"),
    ),
}


def _owner_contract(
    owner_kind: str,
    accepted: frozenset[str],
    create_tool: str,
    contracts: Mapping[str, ArtifactContract],
    *,
    provider_neutral: bool,
    doc: str | None,
) -> dict[str, JsonValue]:
    names = sorted(accepted)
    data: dict[str, JsonValue] = {
        "owner_kind": owner_kind,
        "accepted_names": list(names),
        "create_tool": create_tool,
        "contracts": {name: contracts[name].to_json() for name in names},
    }
    if provider_neutral:
        data["provider_neutral"] = True
    if doc is not None:
        data["doc"] = doc
    return data


def upload_contracts_by_owner() -> dict[str, JsonValue]:
    """Return accepted upload names and byte contracts by upload owner-kind."""
    return {
        "run": _owner_contract(
            "run",
            RUN_ARTIFACT_NAMES,
            CREATE_RUN_UPLOAD_TOOL,
            EXTERNAL_BUILD_CONTRACTS,
            provider_neutral=True,
            doc=EXTERNAL_BUILD_UPLOAD_DOC,
        ),
        "investigations": _owner_contract(
            "investigations",
            SYSTEM_ARTIFACT_NAMES,
            CREATE_INVESTIGATION_UPLOAD_TOOL,
            _INVESTIGATION_CONTRACTS,
            provider_neutral=False,
            doc=None,
        ),
    }


def external_build_contract_document() -> dict[str, JsonValue]:
    """Return the generated external-build requirements document."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "external-build-requirements",
        "upload_contracts": upload_contracts_by_owner(),
        "feature_config_requirements": {
            # The legend rides with the entries rather than living in a doc (ADR-0546 §3): an
            # `enforcement` value with no served definition is the defect #1867 filed.
            "enforcement_legend": enforcement_legend(),
            "features": list(feature_manifest()),
        },
        "docs": {"upload_recipe": EXTERNAL_BUILD_UPLOAD_DOC},
        "next_tools": {
            "create_run_upload": CREATE_RUN_UPLOAD_TOOL,
            "create_investigation_upload": CREATE_INVESTIGATION_UPLOAD_TOOL,
            "complete_run_build": "runs.complete_build",
        },
    }


def external_build_contract_json() -> str:
    """Return the external-build contract as stable, human-readable JSON."""
    return json.dumps(external_build_contract_document(), indent=2, sort_keys=True) + "\n"
