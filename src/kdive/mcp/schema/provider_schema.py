"""Agent-facing provider-schema narrowing + call-time guard (ADR-0269).

Both helpers read the single live ``registered_kinds()`` set, so the published schema
and the accept/reject decision cannot disagree about membership. The projection is a
structural narrowing of the FastMCP-generated schema: the domain models stay static, so
the section sub-models are already present in ``$defs`` and the projection only drops
members.
"""

from __future__ import annotations

import copy
from typing import cast

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provider_sections import aliases_for

_RESOURCE_KIND_DEF = "ResourceKind"
_PROVIDER_SECTION_DEF = "ProviderSection"

type JsonSchema = dict[str, object]


def project_tool_schema(parameters: JsonSchema, kinds: frozenset[ResourceKind]) -> JsonSchema:
    """Return a deep-copy of ``parameters`` narrowed to the composed ``kinds``.

    Filters the ``ResourceKind`` enum to the live kind values and the ``ProviderSection``
    object's properties to the live aliases. A schema with no ``$defs`` (or missing either
    definition) is returned structurally unchanged.

    Args:
        parameters: A FastMCP-generated JSON schema dict (not mutated).
        kinds: The frozenset of currently composed ``ResourceKind`` values.

    Returns:
        A deep copy of ``parameters`` with enum and properties narrowed to ``kinds``.
    """
    projected = copy.deepcopy(parameters)
    defs = projected.get("$defs")
    if not isinstance(defs, dict):
        return projected
    defs = cast(dict[str, object], defs)
    live_values = [k.value for k in ResourceKind if k in kinds]
    kind_def = defs.get(_RESOURCE_KIND_DEF)
    if isinstance(kind_def, dict):
        kind_def = cast(dict[str, object], kind_def)
        enum = kind_def.get("enum")
        if isinstance(enum, list):
            kind_def["enum"] = [value for value in enum if value in live_values]
    section_def = defs.get(_PROVIDER_SECTION_DEF)
    if isinstance(section_def, dict):
        section_def = cast(dict[str, object], section_def)
        properties = section_def.get("properties")
    else:
        properties = None
    if isinstance(properties, dict):
        properties = cast(dict[str, object], properties)
        live = aliases_for(kinds)
        section_def["properties"] = {
            alias: schema for alias, schema in properties.items() if alias in live
        }
    return projected


def assert_kind_composed(kind: ResourceKind, kinds: frozenset[ResourceKind]) -> None:
    """Raise ``CategorizedError(CONFIGURATION_ERROR)`` when ``kind`` is not in ``kinds``.

    Args:
        kind: The ``ResourceKind`` being asserted as configured.
        kinds: The frozenset of currently composed ``ResourceKind`` values.

    Raises:
        CategorizedError: With ``CONFIGURATION_ERROR`` category when ``kind`` is absent.
    """
    if kind in kinds:
        return
    available = sorted(k.value for k in kinds)
    message = (
        "no providers configured"
        if not kinds
        else f"resource kind {kind.value!r} is not configured in this deployment"
    )
    raise CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"kind": kind.value, "available": available},
    )
