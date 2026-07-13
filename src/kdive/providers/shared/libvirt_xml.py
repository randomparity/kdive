"""Shared libvirt XML contract helpers for provider implementations."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection

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


def parse_guest_arches(caps_xml: str, supported: Collection[str]) -> dict[str, dict[str, str]]:
    """Derive the bootable guest arches a host advertises, filtered to ``supported`` (ADR-0338).

    Reads the libvirt capabilities ``<guest>`` blocks and, per bootable ``hvm`` guest arch in
    ``supported``, records the accelerator and emulator libvirt reports for it:

    - ``accel`` is ``"kvm"`` when the arch offers a ``<domain type='kvm'>`` (libvirt advertises a
      KVM domain only when KVM can accelerate that guest arch on this host, so the advertisement
      is the authoritative signal), else ``"tcg"``.
    - ``emulator`` is the arch-level ``<emulator>`` path. A per-``<domain>`` ``<emulator>``
      override (a Xen-era feature) is not handled — kdive is QEMU-only and every real QEMU
      capabilities document places the element at ``<arch>`` level. An arch with no ``<emulator>``
      text is skipped (kdive cannot boot a guest without knowing the binary).

    ``supported`` is injected (the kdive-provisionable arch set,
    :data:`kdive.domain.platform.arch_traits.SUPPORTED_ARCHES`) so this shared helper keeps no
    dependency on ``domain``. Parsed with ``defusedxml`` (the XML crosses the libvirtd trust
    boundary): a malformed or attack document returns ``{}`` so discovery never crashes, mirroring
    :func:`parse_capabilities_arch`. On a duplicate ``<arch name>`` (not seen in practice) the
    first occurrence wins.

    Returns:
        ``{arch: {"accel": "kvm"|"tcg", "emulator": path}}`` — a plain JSON-shaped mapping (the
        typed :class:`~kdive.domain.catalog.resource_capabilities.GuestArch` read side reads it
        back).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        return {}
    result: dict[str, dict[str, str]] = {}
    for guest in root.findall("./guest"):
        if guest.findtext("os_type") != "hvm":
            continue
        for arch in guest.findall("arch"):
            name = arch.get("name")
            if name is None or name not in supported or name in result:
                continue
            emulator = (arch.findtext("emulator") or "").strip()
            if not emulator:
                continue
            accel = "kvm" if arch.find("domain[@type='kvm']") is not None else "tcg"
            result[name] = {"accel": accel, "emulator": emulator}
    return result


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


# The SSH forward kdive renders into a `-netdev user,...hostfwd=...` arg: a host port forwarded
# to the guest's sshd on port 22. local-libvirt binds `127.0.0.1` (ADR-0218 §2); remote-libvirt
# binds the operator-ACL'd `ssh_addr` (ADR-0291). The bind host is captured non-greedily (any
# IPv4/hostname literal — an IPv6 bracket form is a known follow-up) and anchored on the guest
# port `22` so a non-kdive forward is not mistaken for one.
_SSH_HOSTFWD_RE = re.compile(r"hostfwd=tcp:[^:]+:(\d+)-:22")


def recorded_ssh_port_from_root(root: ET.Element) -> int | None:
    """The forwarded SSH host port a parsed domain element records, or ``None``.

    Walks the ``<qemu:commandline>`` args for a ``-netdev`` flag immediately followed by a
    ``user,...hostfwd=tcp:<bind_addr>:<port>-:22`` value and returns the forwarded host port; the
    first matching ``-netdev`` value wins (kdive renders exactly one SSH forward). ``None`` when
    absent or the port text is non-integer. Shared by local-libvirt (loopback bind, ADR-0218 §6)
    and remote-libvirt (ACL'd ``ssh_addr`` bind, ADR-0291); mirrors
    :func:`recorded_gdb_port_from_root`.
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
