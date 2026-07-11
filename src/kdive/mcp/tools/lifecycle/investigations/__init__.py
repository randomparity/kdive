"""Investigation MCP tool package."""

from kdive.mcp.tools.lifecycle.investigations.common import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    ExternalRefInput,
    ExternalRefKey,
)
from kdive.mcp.tools.lifecycle.investigations.lifecycle import (
    close_investigation,
    open_investigation,
)
from kdive.mcp.tools.lifecycle.investigations.metadata import (
    link_external_ref,
    set_investigation,
    unlink_external_ref,
)
from kdive.mcp.tools.lifecycle.investigations.read import (
    get_investigation,
    list_investigations,
)
from kdive.mcp.tools.lifecycle.investigations.registrar import register

__all__ = [
    "DESCRIPTION_MAX",
    "TITLE_MAX",
    "ExternalRefInput",
    "ExternalRefKey",
    "close_investigation",
    "get_investigation",
    "link_external_ref",
    "list_investigations",
    "open_investigation",
    "register",
    "set_investigation",
    "unlink_external_ref",
]
