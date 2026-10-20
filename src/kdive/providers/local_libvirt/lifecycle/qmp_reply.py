"""Parse a local QMP passthrough reply (ADR-0385 amendment, #3270).

``libvirt_qemu.qemuMonitorCommand`` returns QEMU's reply text whatever it contains: a refused
command comes back as ``{"error": {...}}`` on a successful call, not as a ``libvirtError``. Every
local passthrough caller therefore parses the reply here and decides what an error means for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class MalformedQmpReplyError(ValueError):
    """The reply is not a QMP response object with exactly one of ``return`` or ``error``."""


@dataclass(frozen=True, slots=True)
class QmpReply:
    """One parsed QMP response; ``error_class`` is ``None`` for a successful reply."""

    value: object
    reply_id: object
    error_class: str | None = None
    description: str = ""

    @property
    def absent(self) -> bool:
        """An error reply saying the addressed object does not exist."""
        return self.error_class == "DeviceNotFound" or (
            self.error_class is not None and "not found" in self.description.lower()
        )


def parse_qmp_reply(raw: str) -> QmpReply:
    """Parse ``raw``; raise :class:`MalformedQmpReplyError` for any other shape."""
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as error:
        raise MalformedQmpReplyError("reply is not JSON") from error
    if not isinstance(response, dict):
        raise MalformedQmpReplyError("reply is not an object")
    if ("return" in response) == ("error" in response):
        raise MalformedQmpReplyError("reply must carry exactly one of return or error")
    reply_id = response.get("id")
    if "return" in response:
        return QmpReply(value=response["return"], reply_id=reply_id)
    error = response["error"]
    if not isinstance(error, dict) or not isinstance(error.get("class"), str):
        raise MalformedQmpReplyError("error member lacks a string class")
    description = error.get("desc")
    return QmpReply(
        value=None,
        reply_id=reply_id,
        error_class=error["class"],
        description=description if isinstance(description, str) else "",
    )
