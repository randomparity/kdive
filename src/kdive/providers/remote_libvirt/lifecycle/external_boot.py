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
from typing import Annotated, Protocol, Self
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
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult, GuestDomain
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name
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


_XML_REJECTION_CODES = frozenset(
    {libvirt.VIR_ERR_XML_ERROR, libvirt.VIR_ERR_XML_DETAIL, libvirt.VIR_ERR_CONFIG_UNSUPPORTED}
)


class ActivationDomain(Protocol):
    """The libvirt domain surface activation uses."""

    def isActive(self) -> int: ...  # noqa: N802 - binding name

    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - binding name

    def create(self) -> int: ...


class ActivationConn(Protocol):
    """The libvirt connection surface activation uses."""

    def lookupByName(self, name: str) -> ActivationDomain: ...  # noqa: N802 - binding name

    def defineXML(self, xml: str) -> ActivationDomain: ...  # noqa: N802 - binding name


def _activation_conflict(
    reason: str, *, definition: RemoteExternalBootDefinition, **extra: object
) -> CategorizedError:
    details: dict[str, object] = {
        "system_id": definition.binding.system_id,
        "run_id": definition.binding.run_id,
        "activation_id": definition.binding.activation_id,
    }
    details.update(extra)
    return CategorizedError(
        f"remote-libvirt external-boot activation refused: {reason}",
        category=ErrorCategory.CONFLICT,
        details=details,
    )


def _classify_libvirt(
    exc: libvirt.libvirtError, *, definition: RemoteExternalBootDefinition, operation: str
) -> CategorizedError:
    code = exc.get_error_code()
    if code in _XML_REJECTION_CODES:
        # libvirt refusing this definition shape is permanent; re-dispatching would burn the
        # readiness deadline on a write that can never land.
        return _activation_conflict(
            f"libvirt rejected the definition during {operation}",
            definition=definition,
            phase=operation,
        )
    return CategorizedError(
        f"remote-libvirt external-boot {operation} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"system_id": definition.binding.system_id, "libvirt_error_code": code},
    )


def _observed_state(
    domain: ActivationDomain, definition: RemoteExternalBootDefinition
) -> tuple[str, str, str]:
    """Classify the inactive definition by digest, never by bytes."""
    try:
        observed = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(exc, definition=definition, operation="read") from exc
    preserved = preserved_definition_identity(observed)
    boot = boot_projection_identity(observed)
    if preserved == definition.source_definition and boot == definition.source_boot:
        return "source", preserved, boot
    if preserved == definition.target_definition and boot == definition.target_boot:
        return "target", preserved, boot
    return "other", preserved, boot


def activate_definition(conn: ActivationConn, definition: RemoteExternalBootDefinition) -> None:
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
    domain_name = domain_name_for(UUID(definition.binding.system_id))
    try:
        domain = conn.lookupByName(domain_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise CategorizedError(
                "remote-libvirt external-boot domain does not exist",
                category=ErrorCategory.NOT_FOUND,
                details={"system_id": definition.binding.system_id, "domain": domain_name},
            ) from exc
        raise _classify_libvirt(exc, definition=definition, operation="lookup") from exc

    active = bool(domain.isActive())
    which, preserved, boot = _observed_state(domain, definition)
    if which == "other" or (which == "source" and active):
        raise _activation_conflict(
            "the observed definition and power are not an admitted combination",
            definition=definition,
            observed_definition=preserved,
            observed_boot=boot,
            active=active,
        )
    if which == "target":
        if active:
            return
        _start(domain, definition)
        return

    try:
        conn.defineXML(definition.target_xml)
    except libvirt.libvirtError as exc:
        raise _classify_libvirt(exc, definition=definition, operation="define") from exc
    after, preserved, boot = _observed_state(domain, definition)
    if after != "target":
        raise _activation_conflict(
            "the defined target did not read back as the target definition",
            definition=definition,
            phase="readback",
            observed_definition=preserved,
            observed_boot=boot,
        )
    _start(domain, definition)


def _start(domain: ActivationDomain, definition: RemoteExternalBootDefinition) -> None:
    try:
        domain.create()
    except libvirt.libvirtError as exc:
        # The persistent definition now names the external kernel while the guest is not running
        # it, so the caller enters recovery rather than retrying a half-applied write. CONFLICT is
        # already non-retryable, so no terminal flag is needed.
        raise _activation_conflict(
            "the target definition was written but the domain did not start",
            definition=definition,
            phase="start",
            libvirt_error_code=exc.get_error_code(),
        ) from exc


class RemoteGuestIdentity(BaseModel):
    """What one guest actually reports: the shared observation plus the saved command line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    running: RunningKernelObservation
    cmdline: bytes


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
    if len(result.stdout) > MAX_GUEST_READ_BYTES:
        raise _identity_failure(
            f"the guest returned an oversized {what} capture", definition=definition, read=what
        )
    return result.stdout


def _single_field(raw: bytes, *, what: str, definition: RemoteExternalBootDefinition) -> str:
    """One line, one field. `uname -r -m` would return two fields on one line; this rejects that."""
    text = raw.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or any(character.isspace() for character in text):
        raise _identity_failure(
            f"the guest returned a malformed {what}", definition=definition, mismatch=what
        )
    return text


def observe_guest_identity(
    agent_exec: _AgentRunner,
    domain: GuestDomain,
    definition: RemoteExternalBootDefinition,
) -> RemoteGuestIdentity:
    """Prove the running kernel and command line are exactly the ones the plan named.

    One bounded attempt, no waiting: an agent that is not yet answering raises a retryable
    ``TRANSPORT_FAILURE`` from the seam, and the caller's readiness deadline and its retry are the
    wait (#2118 owns both).

    ADR-0583 requires the observation to return the newline-stripped ``/proc/cmdline`` bytes and
    core to compare them. They are returned, and this function also compares them and fails closed.
    Do not read the return value as core enforcement: ``ExternalBootPorts.observe`` cannot carry a
    command line today, so this comparison is the only one that runs.

    Reads go through ``GuestAgentExec`` — the ``guest-exec`` RPC is the only one the repository
    records as available on every catalog image — with the two-program allowlist
    ``OBSERVATION_PROGRAMS``. ``uname`` prints every requested field on one space-separated line,
    so the release and the machine are read separately.
    """
    expected_name = domain_name_for(UUID(definition.binding.system_id))
    if domain.name() != expected_name:
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
    if cmdline != definition.expected_cmdline.encode():
        raise _identity_failure(
            "the running command line is not the plan's",
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
            architecture=machine, release=release, gnu_build_id=build_id
        )
    except ValidationError as exc:
        raise _identity_failure(
            "the guest reported an out-of-contract kernel identity", definition=definition
        ) from exc
    if running != definition.expected_running:
        mismatch = next(
            field
            for field in ("architecture", "release", "gnu_build_id")
            if getattr(running, field) != getattr(definition.expected_running, field)
        )
        raise _identity_failure(
            "the running kernel is not the materialized kernel",
            definition=definition,
            mismatch=mismatch,
        )
    return RemoteGuestIdentity(running=running, cmdline=cmdline)
