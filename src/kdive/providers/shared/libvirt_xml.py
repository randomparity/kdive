"""Shared libvirt XML contract helpers for provider implementations."""

from __future__ import annotations

import re
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
    except (ET.ParseError, DefusedXmlException) as _exc:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def parse_metadata_system_id(meta_xml: str) -> str | None:
    """Read the System id from a kdive metadata XML element; ``None`` if empty/malformed."""
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
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
    except (ET.ParseError, DefusedXmlException) as _exc:
        return None
    return recorded_gdb_port_from_root(root)


# The loopback SSH forward local-libvirt renders into a `-netdev user,...hostfwd=...` arg
# (ADR-0218 §2): a host loopback port forwarded to the guest's sshd on port 22. Anchored on the
# `127.0.0.1` host literal and the guest port `22` so a non-kdive forward is not mistaken for one.
_SSH_HOSTFWD_RE = re.compile(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22")


def recorded_ssh_port_from_root(root: ET.Element) -> int | None:
    """The forwarded loopback SSH port a parsed domain element records, or ``None``.

    Walks the ``<qemu:commandline>`` args for a ``-netdev`` flag immediately followed by a
    ``user,...hostfwd=tcp:127.0.0.1:<port>-:22`` value and returns the forwarded host port; the
    first matching ``-netdev`` value wins (kdive renders exactly one SSH forward). ``None`` when
    absent or the port text is non-integer. Mirrors :func:`recorded_gdb_port_from_root` for the
    SSH transport (ADR-0218 §6, ADR-0039).
    """
    args = [
        arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    for previous, current in zip(args, args[1:], strict=False):
        if previous != "-netdev" or current is None:
            continue
        match = _SSH_HOSTFWD_RE.search(current)
        if match is not None:
            return int(match.group(1))
    return None


def recorded_ssh_port(domain_xml: str) -> int | None:
    """The forwarded loopback SSH port a domain's XML records, or ``None`` if absent/malformed."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        return None
    return recorded_ssh_port_from_root(root)
