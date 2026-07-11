"""Compatibility exports for Investigation service helpers."""

from __future__ import annotations

from kdive.services.investigations.common import (
    DESCRIPTION_MAX,
    TERMINAL_INVESTIGATION,
    TITLE_MAX,
    ExternalRefInput,
    ExternalRefKey,
    get_for_update,
    get_mutable_investigation_locked,
    invalid_external_refs_error,
    invalid_text_error,
    natural_key,
    parse_external_ref_input,
    parse_external_refs,
    refs_jsonb,
    resolve_contributor_investigation,
    validate_text,
)

__all__ = [
    "DESCRIPTION_MAX",
    "TERMINAL_INVESTIGATION",
    "TITLE_MAX",
    "ExternalRefInput",
    "ExternalRefKey",
    "get_for_update",
    "get_mutable_investigation_locked",
    "invalid_external_refs_error",
    "invalid_text_error",
    "natural_key",
    "parse_external_ref_input",
    "parse_external_refs",
    "refs_jsonb",
    "resolve_contributor_investigation",
    "validate_text",
]
