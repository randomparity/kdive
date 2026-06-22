"""Shared libvirt XML contract helpers for provider implementations."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"

_kdive_namespace_registered = False
_qemu_namespace_registered = False


def register_kdive_namespace() -> None:
    global _kdive_namespace_registered
    if _kdive_namespace_registered:
        return
    ET.register_namespace("kdive", KDIVE_METADATA_NS)
    _kdive_namespace_registered = True


def register_qemu_namespace() -> None:
    global _qemu_namespace_registered
    if _qemu_namespace_registered:
        return
    ET.register_namespace("qemu", QEMU_NS)
    _qemu_namespace_registered = True


def parse_capabilities_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>`` from libvirt capabilities XML; ``unknown`` if malformed."""
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError, DefusedXmlException:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def parse_metadata_system_id(meta_xml: str) -> str | None:
    """Read the System id from a kdive metadata XML element; ``None`` if empty/malformed."""
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except ET.ParseError, DefusedXmlException:
        return None
    text = (element.text or "").strip()
    return text or None


def recorded_gdb_port_from_root(root: ET.Element) -> int | None:
    """The gdbstub port a parsed domain element records via ``-gdb tcp:host:port``, or ``None``.

    Walks the ``<qemu:commandline>`` args for a ``-gdb`` flag immediately followed by a
    ``tcp:...:<port>`` value and returns the trailing integer; ``None`` when absent or the port
    text is non-integer. Shared by remote-libvirt (LAN-visible host:port) and local-libvirt
    (loopback) — both record the port the same way (ADR-0079/0080, ADR-0210 §1).
    """
    args = [
        arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    for previous, current in zip(args, args[1:], strict=False):
        if previous != "-gdb" or current is None:
            continue
        _, _, port_text = current.rpartition(":")
        try:
            return int(port_text)
        except ValueError:
            return None
    return None


def recorded_gdb_port(domain_xml: str) -> int | None:
    """The gdbstub port a domain's XML records, or ``None`` if absent/malformed."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError, DefusedXmlException:
        return None
    return recorded_gdb_port_from_root(root)
