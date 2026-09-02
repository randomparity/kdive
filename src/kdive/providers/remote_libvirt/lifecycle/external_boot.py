"""Remote-libvirt external Run-boot activation primitives (ADR-0583, #2110).

Three layers, each testable alone: the pure direct-kernel XML projection and the two ADR-0583
definition identities; a closed ``RemoteExternalBootDefinition`` built by a pure
``prepare_target_definition``; and two operations over injected libvirt and guest-agent seams.

Recovery to the disk/GRUB baseline (#2120), offline module capture and restoration (#2129),
provider-host authority fencing and capability advertisement (#2140) are separately owned. This
module implements no shared port and is not wired into ``ProviderRuntime``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits a trusted tree after a defused parse

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.libvirt_xml import (
    register_kdive_namespace,
    register_qemu_namespace,
)

_BOOT_FIELDS = ("kernel", "initrd", "cmdline")
_PRESERVED_PREFIX = b"kdive-libvirt-preserved-v1"
_BOOT_PROJECTION_PREFIX = b"kdive-libvirt-boot-projection-v1"


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()


def _malformed(reason: str) -> CategorizedError:
    """A bad read of the host's definition: retryable."""
    return CategorizedError(
        f"remote-libvirt domain XML {reason}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
    )


def _permanent(reason: str) -> CategorizedError:
    """A definition that will read back identically on every retry: stop."""
    return CategorizedError(
        f"remote-libvirt domain XML {reason}",
        category=ErrorCategory.CONFLICT,
    )


def parse_domain_xml(domain_xml: str) -> ET.Element:
    """Safely parse an NFC domain definition.

    A malformed or entity-bearing read is ``INFRASTRUCTURE_FAILURE`` and retryable. Non-NFC
    character data and a non-``domain`` root are ``CONFLICT``: for a given domain ``XMLDesc`` is
    deterministic, so re-reading returns the same bytes and a retry can only burn the deadline.
    """
    if unicodedata.normalize("NFC", domain_xml) != domain_xml:
        raise _permanent("must be NFC")
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise _malformed("is malformed or forbidden") from exc
    if root.tag != "domain":
        raise _permanent("must have a domain root")
    return root


def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str:
    """Return ``source`` with only the ADR-0583 direct-boot projection replaced.

    ``<os><boot>`` is deliberately left in place: ADR-0583 excludes only the three boot fields
    from the preserved digest, and libvirt ignores the boot device once ``<kernel>`` is set, so
    removing it would change the preserved digest for no behavioral gain.
    """
    root = parse_domain_xml(source)
    os_element = root.find("os")
    if os_element is None:
        os_element = ET.SubElement(root, "os")
    for tag in _BOOT_FIELDS:
        element = os_element.find(tag)
        if element is not None:
            os_element.remove(element)
    ET.SubElement(os_element, "kernel").text = kernel
    if initrd is not None:
        ET.SubElement(os_element, "initrd").text = initrd
    ET.SubElement(os_element, "cmdline").text = cmdline
    register_kdive_namespace()
    register_qemu_namespace()
    return ET.tostring(root, encoding="unicode")


def preserved_definition_identity(domain_xml: str) -> str:
    """The ADR-0583 preserved digest: everything but the three provider-owned boot fields."""
    root = parse_domain_xml(domain_xml)
    cloned = ET.fromstring(ET.tostring(root, encoding="unicode"))  # noqa: S314 - defused above
    os_element = cloned.find("os")
    if os_element is not None:
        for tag in _BOOT_FIELDS:
            element = os_element.find(tag)
            if element is not None:
                os_element.remove(element)
    for element in cloned.iter():
        if len(element) and element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    canonical = ET.canonicalize(
        ET.tostring(cloned, encoding="unicode"),
        with_comments=False,
        strip_text=False,
        rewrite_prefixes=True,
    ).encode()
    return _digest(_PRESERVED_PREFIX, canonical)


def boot_projection_identity(domain_xml: str) -> str:
    """The ADR-0583 boot projection digest over the three provider-owned boot fields."""
    os_element = parse_domain_xml(domain_xml).find("os")
    value: dict[str, str | None] = {
        tag: os_element.findtext(tag) if os_element is not None else None for tag in _BOOT_FIELDS
    }
    value["schema"] = "libvirt-boot-projection-v1"
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(_BOOT_PROJECTION_PREFIX, payload)
