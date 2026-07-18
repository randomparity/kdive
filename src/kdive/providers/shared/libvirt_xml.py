"""Shared libvirt XML contract helpers for provider implementations."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

_log = logging.getLogger(__name__)

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
    boundary): a malformed or attack document is logged at warning and returns ``{}`` so discovery
    never crashes (the log separates a parse fault from a legitimately-empty host), mirroring
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
        # An empty result is otherwise ambiguous — a legitimately-empty host (no foreign qemu
        # binary) looks the same as a parse fault. Log so an operator can tell them apart.
        _log.warning("could not parse libvirt capabilities for guest arches", exc_info=True)
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


@dataclass(frozen=True, slots=True)
class ParsedHostCpu:
    """The raw host-model CPU fields parsed from a domain-capabilities document (ADR-0368).

    Domain-free: ``arch`` is whatever the block carries (usually absent — the caller supplies the
    host arch parsed elsewhere); ``disabled_features`` are the ``<feature policy='disable'>`` names
    the level disable-guard consumes. Baseline-level derivation lives in ``domain/platform`` and is
    applied by the discovery layer, keeping this shared helper free of a ``providers/shared ->
    domain`` dependency.
    """

    model: str
    vendor: str | None
    arch: str | None
    disabled_features: frozenset[str]


def parse_host_cpu(dom_caps_xml: str) -> ParsedHostCpu | None:
    """Read the ``<cpu><mode name='host-model'>`` block from a domain-capabilities document.

    Returns ``None`` on a parse fault, an unsupported/absent host-model mode, or a host-model block
    with no concrete ``<model>`` text (a host libvirt cannot model) — discovery never crashes and
    never advertises an empty model, mirroring :func:`parse_capabilities_arch`. Parsed with
    ``defusedxml`` (the XML crosses the libvirtd trust boundary).
    """
    try:
        root: ET.Element = _safe_fromstring(dom_caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain capabilities for host cpu", exc_info=True)
        return None
    for mode in root.findall("./cpu/mode"):
        if mode.get("name") != "host-model" or mode.get("supported") == "no":
            continue
        model = (mode.findtext("model") or "").strip()
        if not model:
            return None
        vendor = (mode.findtext("vendor") or "").strip() or None
        arch = (mode.findtext("arch") or "").strip() or None
        disabled = frozenset(
            name
            for feat in mode.findall("feature")
            if feat.get("policy") == "disable" and (name := feat.get("name")) is not None
        )
        return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=disabled)
    return None


def parse_host_capabilities_cpu(caps_xml: str) -> ParsedHostCpu | None:
    """Read the host's own ``<host><cpu>`` from a ``getCapabilities`` document (ADR-0369).

    The passthrough-honest host CPU: a ``host-passthrough`` guest gets exactly this CPU, so it is
    the correct local-x86 ``host_cpu`` source (the host-model block under-reports a passthrough
    guest). Returns ``None`` on a parse fault or a block with no concrete ``<model>``;
    ``disabled_features`` is always empty (the host block carries no disable feature).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse host capabilities for host cpu", exc_info=True)
        return None
    cpu = root.find("./host/cpu")
    if cpu is None:
        return None
    model = (cpu.findtext("model") or "").strip()
    if not model:
        return None
    vendor = (cpu.findtext("vendor") or "").strip() or None
    arch = (cpu.findtext("arch") or "").strip() or None
    return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=frozenset())


def parse_selectable_cpus(dom_caps_xml: str) -> list[str]:
    """Sorted, de-duplicated ``custom``-mode pinnable model names (ADR-0369).

    Includes models whose ``usable`` attribute is ``'yes'`` **or** ``'unknown'``, and excludes
    only explicit ``'no'``.

    **Rationale.** On x86_64 hosts, QEMU probes each custom model against KVM capabilities and
    reports ``yes``/``no`` definitively. On ppc64le (and other non-x86 arches) the QEMU/libvirt
    driver does not implement this probe — it reports every model as ``'unknown'``. ``'unknown'``
    therefore means "QEMU did not check" rather than "this host cannot run it". Excluding
    ``'unknown'`` on ppc64le would leave ``selectable_cpus`` empty even though ``POWER9``,
    ``POWER10``, etc. are perfectly valid pins on a native KVM-HV POWER host (``virsh define``
    with ``<cpu mode='custom'><model>POWER9</model>`` succeeds without error).

    The conservative boundary is ``usable='no'``, which is libvirt's explicit "this host provably
    cannot run this model" signal. Everything else — ``yes`` and ``unknown`` — is admitted.

    Returns ``[]`` on a parse fault, an unsupported custom mode, or an empty set after filtering.
    Discovery omits the arch key rather than advertising ``[]``. Parsed with ``defusedxml``
    (crosses the libvirtd trust boundary).
    """
    try:
        root: ET.Element = _safe_fromstring(dom_caps_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain capabilities for selectable cpus", exc_info=True)
        return []
    models = {
        name.strip()
        for mode in root.findall("./cpu/mode")
        if mode.get("name") == "custom" and mode.get("supported") != "no"
        for model in mode.findall("model")
        if model.get("usable") != "no" and (name := (model.text or "")).strip()
    }
    return sorted(models)


def parse_domain_resolved_cpu(domain_xml: str) -> tuple[str | None, ParsedHostCpu | None]:
    """The ``<cpu mode>`` and concrete resolved ``<cpu><model>`` of a running domain (ADR-0369).

    One defusedxml parse of a running-domain XML (obtained with ``VIR_DOMAIN_XML_UPDATE_CPU``, which
    asks libvirt to expand host-model / a ``custom`` pin to a concrete ``<model>``). Returns
    ``(mode, parsed)``:

    - ``parsed`` is the ``ParsedHostCpu`` when the ``<cpu>`` carries a concrete ``<model>``, else
      ``None`` — an unexpanded ``host-passthrough`` or a TCG machine-default;
    - ``mode`` is the ``<cpu mode>`` attribute (or ``None``), so the caller can distinguish an
      unexpanded ``host-passthrough`` (fall back to the host ``<cpu>``) from a TCG machine-default
      (no ``<cpu>`` — best-effort NULL).

    ``(None, None)`` on a parse fault. ``arch`` is read from ``<os><type arch=…>`` when present.
    """
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except (ET.ParseError, DefusedXmlException) as _exc:
        _log.warning("could not parse domain xml for resolved cpu", exc_info=True)
        return None, None
    cpu = root.find("./cpu")
    mode = cpu.get("mode") if cpu is not None else None
    model = (root.findtext("./cpu/model") or "").strip()
    if not model:
        return mode, None
    vendor = (root.findtext("./cpu/vendor") or "").strip() or None
    os_type = root.find("./os/type")
    arch = (os_type.get("arch") if os_type is not None else None) or None
    return mode, ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=frozenset())


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
