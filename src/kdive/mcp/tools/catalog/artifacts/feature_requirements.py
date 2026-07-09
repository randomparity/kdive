"""``artifacts.feature_config_requirements`` — advisory feature -> CONFIG_* manifest (ADR-0318).

Static, read-only, auth-only (ADR-0117), the sibling of ``artifacts.expected_uploads``. It tells
an external kernel builder which ``CONFIG_*`` each debug/platform feature wants, so the agent can
build them in before uploading. Advisory only: kdive never validates the uploaded config, and an
agent may skip any feature.
"""

from __future__ import annotations

from kdive.kernel_config.requirements import feature_manifest
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue

FEATURE_CONFIG_REQUIREMENTS_TOOL = "artifacts.feature_config_requirements"

_OBJECT_ID = "feature-config-requirements"


def feature_config_requirements() -> ToolResponse:
    """Return the advisory feature -> required ``CONFIG_*`` manifest."""
    features: list[JsonValue] = list(feature_manifest())
    return ToolResponse.success(_OBJECT_ID, "ok", data={"features": features})
