"""Shared libvirt external-boot projection and definition identity (ADR-0583)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - trusted edits follow defused parsing

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace

BOOT_FIELDS = ("kernel", "initrd", "cmdline")
MAX_ARTIFACT_PATH_BYTES = 1_024
_PRESERVED_PREFIX = b"kdive-libvirt-preserved-v1"
_BOOT_PROJECTION_PREFIX = b"kdive-libvirt-boot-projection-v1"


class LibvirtDefinitionError(ValueError):
    """A redacted invalid-definition result with stable retry classification."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.retryable = retryable


def parse_domain_xml(domain_xml: str) -> ET.Element:
    """Safely parse one NFC libvirt domain definition."""
    if unicodedata.normalize("NFC", domain_xml) != domain_xml:
        raise LibvirtDefinitionError("domain XML must be NFC", retryable=False)
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise LibvirtDefinitionError(
            "domain XML is malformed or forbidden", retryable=True
        ) from exc
    if root.tag != "domain":
        raise LibvirtDefinitionError("domain XML must have a domain root", retryable=False)
    return root


def _round_trips_in_xml(value: str) -> bool:
    return all(
        code in (0x09, 0x0A)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or code >= 0x10000
        for code in map(ord, value)
    )


def require_artifact_path(value: str, *, what: str) -> str:
    """Require one bounded canonical absolute POSIX path without exposing its value."""
    if (
        not value
        or unicodedata.normalize("NFC", value) != value
        or not value.startswith("/")
        or value == "/"
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or len(value.encode()) > MAX_ARTIFACT_PATH_BYTES
        or "\0" in value
        or not _round_trips_in_xml(value)
    ):
        raise LibvirtDefinitionError(
            f"{what} path must be a bounded canonical NFC absolute POSIX path",
            retryable=False,
        )
    return value


def _require_cmdline(value: str) -> str:
    # Reject rather than normalize: every accepted scalar reaches libvirt byte-for-byte.
    if unicodedata.normalize("NFC", value) != value or not _round_trips_in_xml(value):
        raise LibvirtDefinitionError(
            "command line must be NFC text representable in XML", retryable=False
        )
    return value


def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str:
    """Replace only the three ADR-0583 direct-boot fields in a domain definition."""
    root = parse_domain_xml(source)
    require_artifact_path(kernel, what="kernel")
    if initrd is not None:
        require_artifact_path(initrd, what="initrd")
    _require_cmdline(cmdline)
    os_element = root.find("os")
    if os_element is None:
        os_element = ET.SubElement(root, "os")
    for tag in BOOT_FIELDS:
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


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()


def preserved_element_identity(root: ET.Element) -> str:
    """Compute ADR-0583's preserved digest from an already validated domain element."""
    cloned = ET.fromstring(ET.tostring(root, encoding="unicode"))  # noqa: S314 - trusted clone
    os_element = cloned.find("os")
    if os_element is not None:
        for tag in BOOT_FIELDS:
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


def boot_projection_element_identity(root: ET.Element) -> str:
    """Compute ADR-0583's boot projection digest from a validated domain element."""
    os_element = root.find("os")
    value: dict[str, str | None] = {
        tag: os_element.findtext(tag) if os_element is not None else None for tag in BOOT_FIELDS
    }
    value["schema"] = "libvirt-boot-projection-v1"
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(_BOOT_PROJECTION_PREFIX, payload)


def preserved_definition_identity(domain_xml: str) -> str:
    """Compute ADR-0583's preserved digest from a safely parsed definition."""
    return preserved_element_identity(parse_domain_xml(domain_xml))


def boot_projection_identity(domain_xml: str) -> str:
    """Compute ADR-0583's boot projection digest from a safely parsed definition."""
    return boot_projection_element_identity(parse_domain_xml(domain_xml))
