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
from typing import Annotated, Self
from uuid import UUID

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import (
    Digest,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    register_kdive_namespace,
    register_qemu_namespace,
)

_BOOT_FIELDS = ("kernel", "initrd", "cmdline")
_PRESERVED_PREFIX = b"kdive-libvirt-preserved-v1"
_BOOT_PROJECTION_PREFIX = b"kdive-libvirt-boot-projection-v1"

# The same unit and number the shared ports module applies to a canonical value
# (`ports/external_boot.py:26,44` measures `len(data)` over bytes).
MAX_DEFINITION_BYTES = 65_536
MAX_ARTIFACT_PATH_BYTES = 1_024


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


_ALL_NULL_BOOT_PROJECTION = _digest(
    _BOOT_PROJECTION_PREFIX,
    json.dumps(
        {"cmdline": None, "initrd": None, "kernel": None, "schema": "libvirt-boot-projection-v1"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode(),
)


def _conflict(reason: str, *, system_id: UUID, rule: str) -> CategorizedError:
    return CategorizedError(
        f"remote-libvirt external-boot source is not the owned disk/GRUB baseline: {reason}",
        category=ErrorCategory.CONFLICT,
        details={"system_id": str(system_id), "rule": rule},
    )


def _is_expected_overlay(disk: ET.Element, *, pool: str, volume: str) -> bool:
    source = disk.find("source")
    driver = disk.find("driver")
    target = disk.find("target")
    return (
        source is not None
        and driver is not None
        and target is not None
        and source.get("pool") == pool
        and source.get("volume") == volume
        and driver.get("type") == "qcow2"
        and target.get("dev") == "vda"
        and target.get("bus") == "virtio"
    )


def require_disk_grub_source(domain_xml: str, *, system_id: UUID, pool: str) -> None:
    """Prove an inactive definition is this System's owned disk/GRUB baseline (ADR-0583).

    Raises ``CONFLICT`` on the first failed rule, with the rule name in ``details``. A source
    already carrying external-boot fields fails the first rule: ADR-0583 admits one only while a
    matching durable activation row owns it, and that row is #2116/#2120 state this module cannot
    read, so the conflict is raised for its caller to resolve.

    ``domain_xml`` must be ``XMLDesc(VIR_DOMAIN_XML_INACTIVE)`` output; ADR-0583 makes live XML
    inadmissible as an identity input. That precondition is the caller's and is not enforced here.
    """
    root = parse_domain_xml(domain_xml)
    if boot_projection_identity(domain_xml) != _ALL_NULL_BOOT_PROJECTION:
        raise _conflict(
            "it already carries external-boot fields", system_id=system_id, rule="boot-projection"
        )
    recorded = root.findtext(f"./metadata/{{{KDIVE_METADATA_NS}}}system")
    if recorded != str(system_id):
        raise _conflict(
            "its kdive metadata names another System", system_id=system_id, rule="system-metadata"
        )
    disks = root.findall("./devices/disk[@device='disk']")
    expected_volume = overlay_volume_name(system_id)
    if len(disks) != 1 or not _is_expected_overlay(disks[0], pool=pool, volume=expected_volume):
        raise _conflict(
            "its boot disk is not the System overlay volume", system_id=system_id, rule="boot-disk"
        )
    os_element = root.find("os")
    boots = os_element.findall("boot") if os_element is not None else []
    if len(boots) != 1 or boots[0].get("dev") != "hd":
        raise _conflict(
            "disk boot is not its only boot selection",
            system_id=system_id,
            rule="boot-selection",
        )
    if os_element is not None and (
        os_element.get("firmware") is not None
        or os_element.find("loader") is not None
        or os_element.find("nvram") is not None
    ):
        raise _conflict(
            "it carries loader, firmware, or NVRAM fields", system_id=system_id, rule="firmware"
        )


def _bounded_definition(value: str) -> str:
    if len(value.encode()) > MAX_DEFINITION_BYTES:
        raise ValueError("domain XML exceeds 65536 bytes")
    return value


class RemoteExternalBootDefinition(BaseModel):
    """The exact source and target definitions for one remote activation.

    Closed and frozen. The recorded digests are revalidated against the recorded XML on every
    construction, so a tampered or corrupted stored record cannot present digests that do not
    describe its own bytes. It round-trips through ordinary pydantic JSON, not the shared ports
    module's canonical encoding: that pair is private to ``_ClosedValue`` and ``providers/ports/``
    is outside this change's surface. Nothing needs it — this value's ADR-0583 identity is the
    preserved digest it records, never a digest over its own serialization.

    It carries no ``ProviderStateIdentity`` and no prior power state: both pair the definition with
    module-tree or recovery evidence that #2129 and #2120 own.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    source_xml: Annotated[str, Field(min_length=1)]
    source_definition: Digest
    source_boot: Digest
    target_xml: Annotated[str, Field(min_length=1)]
    target_definition: Digest
    target_boot: Digest
    expected_running: RunningKernelObservation
    expected_cmdline: Annotated[str, Field(min_length=1)]

    _bounded = field_validator("source_xml", "target_xml")(_bounded_definition)

    @model_validator(mode="after")
    def _digests_recompute(self) -> Self:
        # The identity helpers raise CategorizedError, which pydantic does not convert. Re-raise
        # as ValueError so every construction failure — including rehydration of a corrupted
        # stored record — surfaces as one ValidationError.
        try:
            pairs = (
                (self.source_xml, self.source_definition, self.source_boot),
                (self.target_xml, self.target_definition, self.target_boot),
            )
            for xml, definition, boot in pairs:
                if preserved_definition_identity(xml) != definition:
                    raise ValueError("recorded definition digest does not describe its own XML")
                if boot_projection_identity(xml) != boot:
                    raise ValueError("recorded boot projection does not describe its own XML")
        except CategorizedError as exc:
            raise ValueError(str(exc)) from exc
        return self


def _require_artifact_path(value: str, *, system_id: UUID, what: str) -> str:
    """Shape-check a caller-resolved host path before it is written into the domain XML.

    The caller is a worker trusted to the same degree as this one, so this catches a resolution
    defect rather than an attack. It is stated so a later caller change cannot make the boundary
    load-bearing unnoticed.
    """
    if (
        not value
        or unicodedata.normalize("NFC", value) != value
        or not value.startswith("/")
        or len(value.encode()) > MAX_ARTIFACT_PATH_BYTES
        or "\0" in value
        or ".." in value.split("/")
    ):
        raise _conflict(
            f"{what} path is empty, non-NFC, relative, oversized, or carries a traversal segment",
            system_id=system_id,
            rule="artifact-path",
        )
    return value


def prepare_target_definition(
    source_xml: str,
    *,
    plan: ExternalBootPlan,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
    pool: str,
    kernel_path: str,
    initrd_path: str | None,
) -> RemoteExternalBootDefinition:
    """Derive the exact target definition for one activation. Pure.

    ``source_xml`` must be ``XMLDesc(VIR_DOMAIN_XML_INACTIVE)`` output: ADR-0583 makes live XML
    inadmissible as an identity input, and a definition built from live XML would record digests
    libvirt will never return. That precondition is the caller's and is not enforced here.

    ``kernel_path`` and ``initrd_path`` are resolved by the caller from the opaque references
    #2109 minted, because ADR-0583 forbids a provider path crossing the shared seam and this
    module never learns the host's pool directory. ``plan.cmdline`` is used verbatim, with no
    tokenizing, quoting, normalization, or shell.

    It does not check that the materialization carries a kernel reference:
    ``MaterializedArtifacts.kernel`` is required on a closed frozen model, so one without it
    cannot be constructed and the check could never fail.
    """
    system_id = UUID(binding.system_id)
    if (
        binding.system_id != plan.ownership.system_id
        or binding.system_id != materialization.ownership.system_id
        or binding.run_id != plan.ownership.run_id
        or binding.run_id != materialization.ownership.run_id
    ):
        raise _conflict(
            "binding, plan, and materialization ownership disagree",
            system_id=system_id,
            rule="ownership",
        )
    if materialization.plan_identity != plan.identity:
        raise _conflict(
            "materialization does not describe this plan", system_id=system_id, rule="plan-identity"
        )
    if (plan.initrd is not None) != (materialization.artifacts.initrd is not None) or (
        plan.initrd is not None
    ) != (initrd_path is not None):
        raise _conflict(
            "initrd presence disagrees across plan, materialization, and supplied path",
            system_id=system_id,
            rule="initrd-presence",
        )
    _require_artifact_path(kernel_path, system_id=system_id, what="kernel")
    if initrd_path is not None:
        _require_artifact_path(initrd_path, system_id=system_id, what="initrd")
    require_disk_grub_source(source_xml, system_id=system_id, pool=pool)
    target_xml = render_target_xml(
        source_xml, kernel=kernel_path, initrd=initrd_path, cmdline=plan.cmdline
    )
    return RemoteExternalBootDefinition(
        binding=binding,
        plan_identity=plan.identity,
        materialization_identity=materialization.identity,
        source_xml=source_xml,
        source_definition=preserved_definition_identity(source_xml),
        source_boot=boot_projection_identity(source_xml),
        target_xml=target_xml,
        target_definition=preserved_definition_identity(target_xml),
        target_boot=boot_projection_identity(target_xml),
        expected_running=materialization.kernel_observation,
        expected_cmdline=plan.cmdline,
    )
