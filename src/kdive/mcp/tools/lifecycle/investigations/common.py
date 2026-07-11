"""MCP adapters shared by Investigation tools."""

from __future__ import annotations

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import ConfigErrorReason
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import config_error_reason as _config_error_reason
from kdive.mcp.tools._common import not_found as _not_found
from kdive.services.investigations.common import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    ExternalRefInput,
    ExternalRefKey,
    InvestigationErrorReason,
    InvestigationServiceError,
)


def investigation_error_response(exc: InvestigationServiceError) -> ToolResponse:
    """Map a transport-neutral Investigation service error to the MCP envelope."""
    if exc.reason is InvestigationErrorReason.NOT_FOUND:
        return _not_found(exc.object_id)
    if exc.reason is InvestigationErrorReason.NON_MUTABLE:
        return _config_error(exc.object_id, detail=exc.detail, data=exc.data)
    if exc.reason in {
        InvestigationErrorReason.ABANDONED,
        InvestigationErrorReason.ILLEGAL_STATE,
    }:
        return _config_error(exc.object_id, detail=exc.detail, data=exc.data)
    reason = _CONFIG_REASON[exc.reason]
    return _config_error_reason(
        exc.object_id,
        reason,
        accepted_values=exc.accepted_values,
        detail=exc.detail,
    )


_CONFIG_REASON: dict[InvestigationErrorReason, ConfigErrorReason] = {
    InvestigationErrorReason.INVALID_EXTERNAL_REF: ConfigErrorReason.INVALID_EXTERNAL_REF,
    InvestigationErrorReason.INVALID_TEXT: ConfigErrorReason.INVALID_TEXT,
    InvestigationErrorReason.MISSING_REQUIRED_FIELD: ConfigErrorReason.MISSING_REQUIRED_FIELD,
}


__all__ = [
    "DESCRIPTION_MAX",
    "TITLE_MAX",
    "ExternalRefInput",
    "ExternalRefKey",
    "investigation_error_response",
]
