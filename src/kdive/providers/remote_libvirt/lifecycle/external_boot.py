"""Remote-libvirt external Run-boot activation primitives (ADR-0583, #2110, #2120).

Three layers, each testable alone: the pure direct-kernel XML projection and the two ADR-0583
definition identities; a closed ``RemoteExternalBootDefinition`` built by a pure
``prepare_target_definition``; and three operations over injected libvirt and guest-agent seams —
activation, guest identity proof, and recovery to the recorded disk/GRUB baseline.

Offline module capture and restoration (#2129) and provider-host authority fencing and capability
advertisement (#2140) are separately owned. This module implements no shared port and is not wired
into ``ProviderRuntime``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits a trusted tree after a defused parse
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from kdive.build_artifacts.validation import parse_gnu_build_id
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import (
    Architecture,
    Digest,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    KernelIdentity,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
from kdive.providers.shared.guest_agent import AgentExecResult, GuestDomain
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    register_kdive_namespace,
    register_qemu_namespace,
)
from kdive.providers.shared.runtime_paths import domain_name_for

_BOOT_FIELDS = ("kernel", "initrd", "cmdline")
_PRESERVED_PREFIX = b"kdive-libvirt-preserved-v1"
_BOOT_PROJECTION_PREFIX = b"kdive-libvirt-boot-projection-v1"

# The same unit and number the shared ports module applies to a canonical value
# (`ports/external_boot.py:26,44` measures `len(data)` over bytes).
MAX_DEFINITION_BYTES = 65_536
MAX_ARTIFACT_PATH_BYTES = 1_024
MAX_GUEST_READ_BYTES = 65_536
MAX_CMDLINE_BYTES = 2_048

UNAME_PROGRAM = "/usr/bin/uname"
CAT_PROGRAM = "/usr/bin/cat"
OBSERVATION_PROGRAMS = frozenset({UNAME_PROGRAM, CAT_PROGRAM})
PROC_CMDLINE_PATH = "/proc/cmdline"
KERNEL_NOTES_PATH = "/sys/kernel/notes"

# The shared contract's two architectures. A guest reporting anything else fails identity proof
# here rather than inside the shared model, so the failure names the field.
_ARCHITECTURES: tuple[Architecture, ...] = ("x86_64", "ppc64le")


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


def _is_expected_overlay(
    disk: ET.Element, *, pool: str, volume: str, storage: ET.Element | None
) -> bool:
    source = disk.find("source")
    driver = disk.find("driver")
    target = disk.find("target")
    volume_identity = source is not None and (
        (source.get("pool"), source.get("volume")) == (pool, volume)
        or (
            source.get("file") is not None
            and storage is not None
            and (storage.get("pool"), storage.get("volume")) == (pool, volume)
        )
    )
    return (
        source is not None
        and driver is not None
        and target is not None
        and volume_identity
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

    These rules have been proven only against ``render_domain_xml`` output — what kdive writes,
    not what libvirt returns after parsing it. libvirt can add fields of its own on define, and
    the firmware rule rejects any it added there, so #2121's live tier must capture one real
    ``XMLDesc(VIR_DOMAIN_XML_INACTIVE)`` from a provisioned System, assert this function accepts
    it, and freeze that capture as a fixture.
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
    storage = root.find(f"./metadata/{{{KDIVE_METADATA_NS}}}storage")
    expected_volume = overlay_volume_name(system_id)
    if len(disks) != 1 or not _is_expected_overlay(
        disks[0], pool=pool, volume=expected_volume, storage=storage
    ):
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
    construction, and ``expected_cmdline`` against the target definition's own ``<cmdline>``, so a
    tampered or corrupted stored record cannot present either as describing bytes it does not.
    ``expected_running`` is not bound this way: it comes from the materialization, not from the
    XML, and nothing in this value can attest it.

    It round-trips through ordinary pydantic JSON, not the shared ports
    module's canonical encoding: that pair is private to ``_ClosedValue`` and ``providers/ports/``
    is outside this change's surface. Nothing needs it — this value's ADR-0583 identity is the
    preserved digest it records, never a digest over its own serialization.

    It carries no ``ProviderStateIdentity``: that pairs the definition with the module-tree
    evidence #2129 owns. The prior power state recovery must restore is not here either — it is
    observed at preparation, not derivable from these bytes, and lives on
    ``RemoteExternalBootRecovery``, which is what recovery consumes.
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
    expected_running: KernelIdentity
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
            target_os = parse_domain_xml(self.target_xml).find("os")
            recorded_cmdline = target_os.findtext("cmdline") if target_os is not None else None
            if recorded_cmdline != self.expected_cmdline:
                raise ValueError("expected_cmdline does not match the target definition")
        except CategorizedError as exc:
            raise ValueError(str(exc)) from exc
        return self


def _round_trips_in_xml(value: str) -> bool:
    """True when every character is XML 1.0 character data that reads back unchanged.

    XML 1.0 forbids the C0 controls other than tab, newline, and carriage return, and forbids lone
    surrogates and the two noncharacters; a parser also folds a carriage return into a newline, so
    ``\\r`` is legal but does not round-trip. ``ET.tostring`` emits every one of them without
    complaint, so a value carrying one composes a target XML that this module's own parse gate
    then rejects as malformed — naming the domain XML rather than the value that spoiled it.
    """
    return all(
        code in (0x09, 0x0A)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or code >= 0x10000
        for code in map(ord, value)
    )


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
        or not _round_trips_in_xml(value)
    ):
        raise _conflict(
            f"{what} path is empty, non-NFC, relative, oversized, unrepresentable in XML, "
            "or carries a traversal segment",
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
    # ExternalBootPlan admits a non-NFC debug_cmdline, and ADR-0583 forbids a provider
    # normalizing the command line. Left alone it would compose a target XML that this module's
    # own NFC gate rejects, reporting the domain XML as the subject. Name it here instead.
    if unicodedata.normalize("NFC", plan.cmdline) != plan.cmdline:
        raise _conflict(
            "the plan command line is not NFC and this provider may not normalize it",
            system_id=system_id,
            rule="cmdline-nfc",
        )
    # Same shape, different gate: ExternalBootPlan admits a platform argument carrying an XML-
    # illegal C0 control (it rejects only NUL and whitespace), which composes a target XML that
    # parse_domain_xml then reports as a malformed *domain XML* — a retryable
    # INFRASTRUCTURE_FAILURE with no rule and no system_id, so the caller re-dispatches and burns
    # the readiness deadline re-deriving a definition that can never parse. Name it here instead.
    if not _round_trips_in_xml(plan.cmdline):
        raise _conflict(
            "the plan command line carries a character XML cannot represent",
            system_id=system_id,
            rule="cmdline-xml",
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


_XML_REJECTION_CODES = frozenset(
    {libvirt.VIR_ERR_XML_ERROR, libvirt.VIR_ERR_XML_DETAIL, libvirt.VIR_ERR_CONFIG_UNSUPPORTED}
)


class ExternalBootDomain(Protocol):
    """The libvirt domain surface activation and recovery use. Only recovery calls ``destroy``."""

    def isActive(self) -> int: ...  # noqa: N802 - binding name

    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - binding name

    def create(self) -> int: ...

    def destroy(self) -> int: ...


class ExternalBootConn(Protocol):
    """The libvirt connection surface activation and recovery use."""

    def lookupByName(self, name: str) -> ExternalBootDomain: ...  # noqa: N802 - binding name

    def defineXML(self, xml: str) -> ExternalBootDomain: ...  # noqa: N802 - binding name


type _Subject = Literal["activation", "recovery"]


def _refused(
    reason: str, *, subject: _Subject, definition: RemoteExternalBootDefinition, **extra: object
) -> CategorizedError:
    details: dict[str, object] = {
        "system_id": definition.binding.system_id,
        "run_id": definition.binding.run_id,
        "activation_id": definition.binding.activation_id,
    }
    details.update(extra)
    return CategorizedError(
        f"remote-libvirt external-boot {subject} refused: {reason}",
        category=ErrorCategory.CONFLICT,
        details=details,
    )


def _classify_libvirt(
    exc: libvirt.libvirtError,
    *,
    definition: RemoteExternalBootDefinition,
    operation: str,
    subject: _Subject,
) -> CategorizedError:
    code = exc.get_error_code()
    if code in _XML_REJECTION_CODES:
        # libvirt refusing this definition shape is permanent; re-dispatching would burn the
        # readiness deadline on a write that can never land.
        return _refused(
            f"libvirt rejected the definition during {operation}",
            subject=subject,
            definition=definition,
            phase=operation,
        )
    return CategorizedError(
        f"remote-libvirt external-boot {operation} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"system_id": definition.binding.system_id, "libvirt_error_code": code},
    )


def _lookup(
    conn: ExternalBootConn, definition: RemoteExternalBootDefinition, *, subject: _Subject
) -> ExternalBootDomain:
    """Resolve the System's domain, separating "no such domain" from every other libvirt fault."""
    domain_name = domain_name_for(UUID(definition.binding.system_id))
    try:
        return conn.lookupByName(domain_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise CategorizedError(
                "remote-libvirt external-boot domain does not exist",
                category=ErrorCategory.NOT_FOUND,
                details={"system_id": definition.binding.system_id, "domain": domain_name},
            ) from exc
        raise _classify_libvirt(
            exc, definition=definition, operation="lookup", subject=subject
        ) from exc


def _is_active(
    domain: ExternalBootDomain, definition: RemoteExternalBootDefinition, *, subject: _Subject
) -> bool:
    """Read power state. A stale handle or dropped connection must not escape uncategorized."""
    try:
        return bool(domain.isActive())
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(
            exc, definition=definition, operation="power-read", subject=subject
        ) from exc


def _observed_state(
    domain: ExternalBootDomain, definition: RemoteExternalBootDefinition, *, subject: _Subject
) -> tuple[str, str, str]:
    """Classify the inactive definition by digest, never by bytes."""
    try:
        observed = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(
            exc, definition=definition, operation="read", subject=subject
        ) from exc
    preserved = preserved_definition_identity(observed)
    boot = boot_projection_identity(observed)
    if preserved == definition.source_definition and boot == definition.source_boot:
        return "source", preserved, boot
    if preserved == definition.target_definition and boot == definition.target_boot:
        return "target", preserved, boot
    return "other", preserved, boot


def activate_definition(conn: ExternalBootConn, definition: RemoteExternalBootDefinition) -> None:
    """Compare-and-set the System's persistent definition to the external-boot target.

    Requires the domain inactive with the recorded source definition, defines the target, verifies
    the readback, then starts it. Comparison is ADR-0583's two-part digest pair, never raw bytes:
    ``defineXML`` parses and ``XMLDesc`` regenerates, so the bytes handed to libvirt are not the
    bytes it returns.

    Idempotent on the achieved post-state — an already-target, already-running domain returns
    without a write, so a retry after a lost response converges instead of redefining.

    ADR-0583's pre-write gate also requires proof of current exclusive mutation authority
    immediately before this write. That proof is #2140's; this function takes the connection its
    caller already fenced and performs the state half of the gate.
    """
    domain = _lookup(conn, definition, subject="activation")
    active = _is_active(domain, definition, subject="activation")
    which, preserved, boot = _observed_state(domain, definition, subject="activation")
    if which == "other" or (which == "source" and active):
        raise _refused(
            "the observed definition and power are not an admitted combination",
            subject="activation",
            definition=definition,
            observed_definition=preserved,
            observed_boot=boot,
            active=active,
        )
    if which == "target":
        if active:
            return
        _start(
            domain,
            definition,
            subject="activation",
            reason="the target definition was already present but the domain did not start",
            phase="start",
        )
        return

    try:
        conn.defineXML(definition.target_xml)
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(
            exc, definition=definition, operation="define", subject="activation"
        ) from exc
    after, preserved, boot = _observed_state(domain, definition, subject="activation")
    if after != "target":
        raise _refused(
            "the defined target did not read back as the target definition",
            subject="activation",
            definition=definition,
            phase="readback",
            observed_definition=preserved,
            observed_boot=boot,
        )
    _start(
        domain,
        definition,
        subject="activation",
        reason="the target definition was written but the domain did not start",
        phase="start-after-define",
    )


def _start(
    domain: ExternalBootDomain,
    definition: RemoteExternalBootDefinition,
    *,
    subject: _Subject,
    reason: str,
    phase: str,
) -> None:
    """Start the domain, reporting truthfully what this invocation had already done.

    A failed start always leaves the persistent definition naming a kernel the guest is not
    running, so the caller enters recovery rather than retrying. CONFLICT is already
    non-retryable, so no terminal flag is needed. ``phase`` distinguishes an invocation that wrote
    the definition from one that found it already written, because a caller deciding between
    retry and recovery is misled by a write that did not happen.
    """
    try:
        domain.create()
    except libvirt.libvirtError as exc:
        raise _refused(
            reason,
            subject=subject,
            definition=definition,
            phase=phase,
            libvirt_error_code=exc.get_error_code(),
        ) from exc


class RemoteExternalBootRecovery(BaseModel):
    """One activation's definition pair plus the power state recovery must restore.

    Closed and frozen. ``prior_power`` is the domain's power state as observed at preparation,
    before any external-boot write; it is not derivable from either definition, so it is recorded
    here rather than on ``RemoteExternalBootDefinition``. ADR-0583 makes it the condition recovery
    has to reach: a System that was running before activation is recovered only once it is running
    its disk/GRUB baseline again, and one that was stopped is recovered when the baseline
    definition is verified inactive.

    It carries no module-tree component: ADR-0583's provider state identity pairs the definition
    with a release-qualified module identity, and that half is #2129's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: RemoteExternalBootDefinition
    prior_power: Literal["running", "inactive"]


def _stop(domain: ExternalBootDomain, definition: RemoteExternalBootDefinition) -> None:
    """Stop a domain running the external kernel, tolerating a destroy that already landed.

    ``destroy`` is used rather than a graceful shutdown: the guest is running a debug kernel that
    may not answer ACPI, and ADR-0583 makes verified inactivity — not guest cooperation — the
    precondition for the source write.
    """
    try:
        domain.destroy()
    except libvirt.libvirtError as exc:
        # A lost response can leave the destroy applied, and a worker resuming after that must not
        # fail a stop that already took effect. If the re-observation itself fails it is reported
        # as itself, chained to the destroy error so both causes survive.
        try:
            still_active = _is_active(domain, definition, subject="recovery")
        except CategorizedError as observation:
            raise observation from exc
        if still_active:
            raise _classify_libvirt(
                exc, definition=definition, operation="stop", subject="recovery"
            ) from exc


def _restore_source_definition(
    conn: ExternalBootConn, domain: ExternalBootDomain, definition: RemoteExternalBootDefinition
) -> None:
    """Stop the target and compare-and-set the persistent definition back to the source."""
    if _is_active(domain, definition, subject="recovery"):
        _stop(domain, definition)
        which, preserved, boot = _observed_state(domain, definition, subject="recovery")
        still_active = _is_active(domain, definition, subject="recovery")
        if which != "target" or still_active:
            # Both halves are reported: a caller told only the digests cannot tell a competing
            # redefine from a competing restart, and they take different operator resolutions.
            raise _refused(
                "the domain did not stop on its recorded target definition",
                subject="recovery",
                definition=definition,
                phase="stop",
                observed_definition=preserved,
                observed_boot=boot,
                active=still_active,
            )
    try:
        conn.defineXML(definition.source_xml)
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(
            exc, definition=definition, operation="define", subject="recovery"
        ) from exc
    which, preserved, boot = _observed_state(domain, definition, subject="recovery")
    if which != "source":
        raise _refused(
            "the restored baseline did not read back as the recorded source definition",
            subject="recovery",
            definition=definition,
            phase="readback",
            observed_definition=preserved,
            observed_boot=boot,
        )


def recover_disk_grub_baseline(
    conn: ExternalBootConn, recovery: RemoteExternalBootRecovery
) -> None:
    """Restore the System's persistent definition and power state to its disk/GRUB baseline.

    Compare-and-set in the other direction from ``activate_definition``, over the same two-part
    digest pair and never over raw bytes. From the recorded target it stops the domain, defines the
    recorded source, verifies the readback, then restores ``prior_power``; from the recorded source
    it only restores power.

    Idempotent on the achieved post-state, which is what makes it resumable after worker loss: a
    domain already on the baseline in its recorded prior power returns without a write, whichever
    step the lost worker died in. Every step re-observes rather than trusting what a previous
    observation or a previous worker saw.

    Any other observed definition, and a baseline running when the recorded prior power was
    stopped, are ADR-0583's unproven mixtures: they raise ``CONFLICT`` with the observed digests
    retained, for the operator resolution path, rather than overwriting provider state.

    Two halves of ADR-0583 recovery are deliberately not here. Restoring the prior module tree is
    #2129's, and the fresh baseline readiness core requires before it may commit ``recovered`` for
    a System that was running is the caller's (#2118) — this function returns once the baseline is
    defined and started, not once the guest has answered.
    """
    definition = recovery.definition
    domain = _lookup(conn, definition, subject="recovery")
    which, preserved, boot = _observed_state(domain, definition, subject="recovery")
    if which == "other":
        raise _refused(
            "the observed definition is neither the recorded source nor the recorded target",
            subject="recovery",
            definition=definition,
            observed_definition=preserved,
            observed_boot=boot,
        )
    if which == "target":
        _restore_source_definition(conn, domain, definition)
    active = _is_active(domain, definition, subject="recovery")
    if recovery.prior_power == "inactive":
        if active:
            raise _refused(
                "the baseline is running and the recorded prior power state was not",
                subject="recovery",
                definition=definition,
                # Proven equal to what was just observed: this branch is reached only after the
                # definition classified as the recorded source.
                observed_definition=definition.source_definition,
                observed_boot=definition.source_boot,
                active=True,
            )
        return
    if not active:
        _start(
            domain,
            definition,
            subject="recovery",
            reason="the disk/GRUB baseline was restored but the domain did not start",
            phase="start-after-recover",
        )


class _AgentRunner(Protocol):
    def run(
        self, domain: GuestDomain, argv: list[str], *, input_data: str | None = None
    ) -> AgentExecResult: ...


def _identity_failure(
    reason: str, *, definition: RemoteExternalBootDefinition, **extra: object
) -> CategorizedError:
    """READINESS_FAILURE is retryable by category, so identity failures set terminal.

    Without it a guest that booted the wrong kernel would be re-dispatched to observe the same
    wrong guest until the deadline expired, which is the one condition this proof exists to stop.
    Details name which field differed, never the observed value: the guest controls those bytes.
    """
    details: dict[str, object] = {
        "system_id": definition.binding.system_id,
        "run_id": definition.binding.run_id,
        "activation_id": definition.binding.activation_id,
    }
    details.update(extra)
    return CategorizedError(
        f"remote-libvirt external-boot identity proof failed: {reason}",
        category=ErrorCategory.READINESS_FAILURE,
        details=details,
        terminal=True,
    )


def _guest_read(
    agent_exec: _AgentRunner,
    domain: GuestDomain,
    argv: list[str],
    *,
    what: str,
    definition: RemoteExternalBootDefinition,
    max_bytes: int = MAX_GUEST_READ_BYTES,
) -> bytes:
    """Run one read. A CategorizedError from the seam propagates with its own category."""
    result = agent_exec.run(domain, argv)
    if result.exit_status != 0:
        raise _identity_failure(
            f"the guest could not read {what}",
            definition=definition,
            read=what,
            exit_status=result.exit_status,
        )
    if len(result.stdout) > max_bytes:
        raise _identity_failure(
            f"the guest returned an oversized {what} capture", definition=definition, read=what
        )
    return result.stdout


# The shared KernelRelease pattern caps a release at 64 characters, so anything longer cannot be
# valid. Bounding here keeps guest-chosen text out of pydantic, whose ValidationError embeds the
# rejected input verbatim in its message.
MAX_GUEST_FIELD_CHARS = 64


def _single_field(raw: bytes, *, what: str, definition: RemoteExternalBootDefinition) -> str:
    """One line, one field. `uname -r -m` would return two fields on one line; this rejects that."""
    text = raw.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    if (
        not text
        or len(text) > MAX_GUEST_FIELD_CHARS
        or "\n" in text
        or any(character.isspace() for character in text)
    ):
        raise _identity_failure(
            f"the guest returned a malformed {what}", definition=definition, mismatch=what
        )
    return text


def observe_guest_identity(
    agent_exec: _AgentRunner,
    domain: GuestDomain,
    definition: RemoteExternalBootDefinition,
) -> RunningKernelObservation:
    """Return the running kernel identity and exact saved command-line bytes.

    One bounded attempt, no waiting: an agent that is not yet answering raises a retryable
    ``TRANSPORT_FAILURE`` from the seam, and the caller's readiness deadline and its retry are the
    wait (#2118 owns both).

    ADR-0583 requires the observation to return the newline-stripped ``/proc/cmdline`` bytes and
    core to compare them with the target definition's expected bytes. This provider validates the
    expected value against the target XML when the definition is constructed; it does not perform
    the command-line comparison itself.

    Reads go through ``GuestAgentExec`` — the ``guest-exec`` RPC is the only one the repository
    records as available on every catalog image — with the two-program allowlist
    ``OBSERVATION_PROGRAMS``. ``uname`` prints every requested field on one space-separated line,
    so the release and the machine are read separately.
    """
    expected_name = domain_name_for(UUID(definition.binding.system_id))
    try:
        observed_name = domain.name()
    except libvirt.libvirtError as exc:
        # A stale handle or dropped connection must not escape the isolation guard uncategorized.
        raise CategorizedError(
            "remote-libvirt external-boot observation could not read the domain name",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": definition.binding.system_id},
        ) from exc
    if observed_name != expected_name:
        raise CategorizedError(
            "remote-libvirt external-boot observation was given another System's domain",
            category=ErrorCategory.CONFLICT,
            details={"system_id": definition.binding.system_id, "rule": "domain-binding"},
        )

    release = _single_field(
        _guest_read(
            agent_exec, domain, [UNAME_PROGRAM, "-r"], what="release", definition=definition
        ),
        what="release",
        definition=definition,
    )
    machine = _single_field(
        _guest_read(
            agent_exec, domain, [UNAME_PROGRAM, "-m"], what="machine", definition=definition
        ),
        what="architecture",
        definition=definition,
    )
    cmdline = _guest_read(
        agent_exec,
        domain,
        [CAT_PROGRAM, PROC_CMDLINE_PATH],
        what="the kernel command line",
        definition=definition,
        max_bytes=MAX_CMDLINE_BYTES + 1,
    )
    # ADR-0583 removes exactly one trailing newline and treats truncation as terminal. A
    # /proc/cmdline read that does not end in a newline is truncated, not merely unterminated.
    if not cmdline.endswith(b"\n"):
        raise _identity_failure(
            "the kernel command line read was truncated",
            definition=definition,
            mismatch="cmdline",
        )
    cmdline = cmdline[:-1]
    if len(cmdline) > MAX_CMDLINE_BYTES:
        raise _identity_failure(
            "the guest returned an oversized kernel command line capture",
            definition=definition,
            mismatch="cmdline",
        )
    notes = _guest_read(
        agent_exec,
        domain,
        [CAT_PROGRAM, KERNEL_NOTES_PATH],
        what="the kernel notes",
        definition=definition,
    )
    try:
        build_id = parse_gnu_build_id(notes)
    except CategorizedError as exc:
        # parse_gnu_build_id raises BUILD_FAILURE, whose message names a vmlinux that is not
        # involved here. A running guest with unreadable kernel notes has failed identity proof.
        raise _identity_failure(
            "the running kernel has no readable GNU build id",
            definition=definition,
            mismatch="gnu_build_id",
        ) from exc

    if machine not in _ARCHITECTURES:
        raise _identity_failure(
            "the guest reported an architecture the shared contract does not name",
            definition=definition,
            mismatch="architecture",
        )
    try:
        running = RunningKernelObservation(
            identity={"architecture": machine, "release": release, "gnu_build_id": build_id},
            cmdline=cmdline,
            expected_cmdline=definition.expected_cmdline.encode(),
        )
    except ValidationError:
        # Deliberately not chained: pydantic's message embeds the rejected guest value verbatim,
        # and no guest byte may reach a message, a details payload, or a logged traceback.
        raise _identity_failure(
            "the guest reported an out-of-contract kernel identity", definition=definition
        ) from None
    if running.identity != definition.expected_running:
        mismatch = next(
            field
            for field in ("architecture", "release", "gnu_build_id")
            if getattr(running.identity, field) != getattr(definition.expected_running, field)
        )
        raise _identity_failure(
            "the running kernel is not the materialized kernel",
            definition=definition,
            mismatch=mismatch,
        )
    return running
