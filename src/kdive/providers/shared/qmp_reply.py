"""Parse a QMP passthrough reply (ADR-0385 amendment, #3270; remote callers #3294).

``libvirt_qemu.qemuMonitorCommand`` returns QEMU's reply text whatever it contains: a refused
command comes back as ``{"error": {...}}`` on a successful call, not as a ``libvirtError``. Every
local and remote passthrough caller therefore parses the reply here and decides what an error
means for it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# QMP error classes are short CamelCase tokens (GenericError, DeviceNotFound). The class is the one
# QEMU string that reaches failure details, so anything else is a malformed reply.
_ERROR_CLASS = re.compile(r"[A-Za-z]{1,64}")
_PCAP_OPEN_REFUSAL = "net dump: can't open "
_EACCES_SUFFIX = ": Permission denied"
# net/filter.c netfilter_complete(): every netfilter is refused on a vhost-backed netdev.
_VHOST_REFUSAL = "Vhost is not supported"


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

    @property
    def pcap_open_denied(self) -> bool:
        """A ``filter-dump`` ``object-add`` refused: the hypervisor could not open the pcap."""
        return (
            self.error_class is not None
            and self.description.startswith(_PCAP_OPEN_REFUSAL)
            and self.description.endswith(_EACCES_SUFFIX)
        )

    @property
    def vhost_refused(self) -> bool:
        """A netfilter ``object-add`` refused because the netdev is vhost-backed."""
        return self.error_class is not None and self.description == _VHOST_REFUSAL


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
    if not isinstance(error, dict):
        raise MalformedQmpReplyError("error member is not an object")
    error_class = error.get("class")
    if not isinstance(error_class, str) or not _ERROR_CLASS.fullmatch(error_class):
        raise MalformedQmpReplyError("error member lacks a QMP error class token")
    description = error.get("desc")
    return QmpReply(
        value=None,
        reply_id=reply_id,
        error_class=error_class,
        description=description if isinstance(description, str) else "",
    )
