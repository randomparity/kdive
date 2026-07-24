"""The provisioning-profile schema and its parse boundary (ADR-0011, ADR-0024).

A provisioning profile is a versioned, declarative document with a
provider-agnostic core (target arch, vCPU, memory, disk, boot method, kernel-source
reference) and a provider-specific section keyed by provider name. The production
default is ``local-libvirt``; ``fault-inject`` is implemented as an opt-in provider
behind ``ProviderResolver`` for test and failure-path coverage.

The models are ``frozen`` (the immutable-request-inputs invariant, ADR-0003/0011)
and reject unknown fields. :meth:`ProvisioningProfile.parse` is the sanctioned entry
point: it maps Pydantic's structural ``ValidationError`` onto the wire taxonomy's
``configuration_error`` and scrubs submitted values out of the error details so a
profile that references secret or guest-derived material cannot leak it (ADR-0024
decision 3). Constructing a model directly bypasses this mapping and is a caller
error.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.sizing import AllocationSizing
from kdive.domain.profile_documents import SerializedProvisioningProfile
from kdive.profiles._schema import schema_version_validator
from kdive.profiles.types import ProvisioningProfileInput

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""


def _validate_crashkernel_token(value: str) -> str:
    """Reject a crashkernel token that would inject extra kernel cmdline tokens.

    The reservation token is rendered verbatim into the boot ``<cmdline>`` — the install lane
    (ADR-0300) and, since ADR-0390, the provision baseline for a warm-own-kernel System. So
    internal whitespace would inject an extra kernel token into the space-joined cmdline, a
    non-printable character would fail XML rendering of the domain ``<cmdline>``, and a leading
    ``crashkernel=`` prefix would double the key. Mirrors ``InstallPayload._safe_crashkernel``
    and the ``runs.*`` tool-boundary check (the booted kernel remains the arbiter of the size
    grammar itself). Blank is already rejected by :data:`NonEmptyStr`.
    """
    stripped = value.strip()
    if stripped.split() != [stripped]:
        raise ValueError("crashkernel must be a single token with no internal whitespace")
    if not stripped.isprintable():
        raise ValueError("crashkernel must be a single printable token")
    if stripped.lower().startswith("crashkernel="):
        raise ValueError("crashkernel must not include the 'crashkernel=' prefix")
    return stripped


type CrashkernelToken = Annotated[NonEmptyStr, AfterValidator(_validate_crashkernel_token)]
"""A kdump ``crashkernel`` reservation token safe to render into a boot cmdline (ADR-0390)."""

SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})

# fadump minimum guest RAM (ADR-0363, #1181). On POWER, fadump reserves a boot-memory region on
# top of the crashkernel reservation and re-registers on first boot; at 2 GiB the reservation plus
# crashkernel leaves too little for userspace to reach the kdive-ready marker (kdump on the same
# guest passes at 2 GiB, #1156). 4 GiB is the floor the native-POWER fadump proof provisions at.
FADUMP_MIN_MEMORY_MB = 4096


# Provenance: ADR-0024 decision 2a, ADR-0080; disk-image lane ADR-0078.
class BootMethod(StrEnum):
    """The provider-agnostic boot methods.

    ``disk-image`` boots an operator-staged base-OS image and iterates kernels by
    in-guest install + reboot (the remote-libvirt model); ``direct-kernel``
    stays the local-libvirt/fault-inject method.
    """

    DIRECT_KERNEL = "direct-kernel"
    DISK_IMAGE = "disk-image"


class _ProfileBase(BaseModel):
    """Shared config: reject unknown fields and freeze after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _UploadRootfs(_ProfileBase):
    # A System-owned uploaded qcow2; opened by systems.define + artifacts.create_system_upload and
    # committed at provisioning->ready (ADR-0048 §5). local/artifact/catalog are alternatives.
    kind: Literal["upload"]


type RootfsSource = Annotated[
    LocalComponentRef | ArtifactComponentRef | CatalogComponentRef | _UploadRootfs,
    Field(discriminator="kind"),
]
"""A discriminated rootfs source (ADR-0065); ``upload`` remains System-owned."""


# Provenance: ADR-0049 Decision 3.
class LibvirtDebugOptions(_ProfileBase):
    """Per-System debug provisioning flags.

    Bound at provision/boot; declare which capture methods the System is
    provisioned for. ``preserve_on_crash`` adds a pvpanic device +
    ``<on_crash>preserve</on_crash>``; ``gdbstub`` adds the QEMU ``-gdb`` argument;
    ``fadump`` opts a ppc64le System into firmware-assisted dump (adds ``fadump=on`` to the
    boot cmdline, requires a ``crashkernel`` reservation, and a host QEMU that supports it).
    """

    preserve_on_crash: bool = False
    gdbstub: bool = False
    # Provenance: ADR-0349, #1151. Firmware-assisted dump on POWER pseries. Adds ``fadump=on`` to
    # the boot cmdline alongside the ``crashkernel`` reservation; POWER-only and reservation-
    # required (enforced by ``ProvisioningProfile._require_ppc64le_and_reservation_for_fadump``).
    fadump: bool = False


# Provenance: ADR-0369, #1227.
class LibvirtCpuPin(_ProfileBase):
    """An agent-selected guest CPU model pin.

    ``model`` must be one of the bound host's advertised ``selectable_cpus[arch]`` (validated at
    admission); the guest is then pinned to that CPU model. Omit ``cpu`` entirely for the operator
    default (the host CPU: host-passthrough on x86, host-model on ppc64le, the machine default under
    TCG emulation).
    """

    model: NonEmptyStr = Field(
        description=(
            "Guest CPU model to pin, from this host's resources.describe `selectable_cpus[arch]`. "
            "Pin a portable `x86-64-vN` rung for a deterministic reproducer. A model below the "
            "rootfs image's ISA floor (x86-64-v2 for EL9/RHEL-family) produces a NON-BOOTING "
            "System — admission checks only that the host can deliver the model, not that the "
            "image can run on it. Omit to get the operator default (host CPU)."
        )
    )


# Provenance: ADR-0024 decisions 1/2b/2c; rootfs ADR-0048 §3; destructive opt-in ADR-0028 §2;
# debug ADR-0049 Decision 3.
class LibvirtProfile(_ProfileBase):
    """The ``local-libvirt`` provider section.

    ``domain_xml_params`` is an optionally-empty map whose values are non-empty;
    ``rootfs`` is the discriminated rootfs source keyed by ``kind`` —
    ``local`` (an allowlisted provider-local file), ``artifact`` (parsed for the shared
    component contract but currently rejected by local-libvirt materialization),
    ``catalog`` (a curated image by name), or ``upload`` (a System-owned uploaded
    object); the resolver maps supported references to the libvirt-readable disk path
    at provisioning. ``crashkernel`` is an
    optional opaque non-empty token (the kdump prerequisite — the booted kernel is the
    arbiter of its grammar); ``None`` when the System is not provisioned for kdump.
    ``baseline_kernel`` is an optional hint naming the baseline kernel to boot when the
    rootfs ``/boot`` holds more than one kernel; ``None`` (the common single-kernel case) keeps
    fail-closed selection.
    ``destructive_ops`` is the optionally-empty opt-in list for ``force_crash``
    (e.g. ``["force_crash"]``); the control plane's gate resolves the opt-in factor from it
    (deny-by-default — an absent or empty list refuses it). ``control.power`` and
    ``systems.reprovision`` are contributor leaseholder lifecycle and are not gated by it.
    ``debug`` declares
    which crash-capture methods the System is provisioned for; defaults to all flags disabled.
    """

    domain_xml_params: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    rootfs: RootfsSource
    crashkernel: CrashkernelToken | None = None
    baseline_kernel: NonEmptyStr | None = Field(
        default=None,
        # Provenance: ADR-0310, #1016.
        description=(
            "Optional hint naming the baseline kernel to boot when the rootfs /boot holds more "
            "than one kernel. A direct-kernel provision extracts the rootfs's own kernel and fails "
            "closed on an ambiguous multi-kernel /boot rather than guessing a version order; this "
            "hint is the explicit escape hatch. Give either the full 'vmlinuz-<ver>' filename or "
            "the bare '<ver>' (copy a value from the 'candidates' list in the ambiguous-selection "
            "error). A hint naming no present kernel is rejected. Omit it for a single-kernel "
            "image (the common case) — selection is then unambiguous."
        ),
    )
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    debug: LibvirtDebugOptions = Field(default_factory=LibvirtDebugOptions)
    cpu: LibvirtCpuPin | None = None


# Provenance: ADR-0072.
class FaultInjectProfile(_ProfileBase):
    """The ``fault-inject`` provider section.

    The mock provider owns no rootfs/domain XML materialization; its section carries only
    the knobs shared by generic control/retrieve gates.
    """

    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    capture_method: CaptureMethod = CaptureMethod.CONSOLE


# Provenance: ADR-0080; image-content obligations ADR-0078/0079; supplied rootfs ADR-0440 (#1433);
# destructive opt-in ADR-0028 §2; gdbstub port ADR-0079/0080; host_dump opt-in ADR-0426/0094.
class RemoteLibvirtProfile(_ProfileBase):
    """The ``remote-libvirt`` provider section.

    The base image is named **one** of two ways (exactly one is required):

    - ``base_image_volume`` names an **operator-staged** qcow2 volume already present on the
      remote host's storage pool; provisioning references it by name and verifies it exists.
    - ``base_image_source`` supplies a worker-host ``local`` qcow2 (a ``LocalComponentRef``,
      the same ``local`` source kind remote accepts for kernel/vmlinux) which provisioning stages
      onto the pool per-System via the volume-upload primitive.

    Either way the base image carries the **image-content obligations the supplier owns**: the
    base OS with qemu-guest-agent enabled, drgn, and matching vmlinux/debuginfo. Provisioning
    verifies the volume exists and — for a supplied qcow2 — that its bytes start with the qcow2
    magic; it does **not** verify these deeper obligations, which are not introspectable from a
    volume lookup. A base image missing them provisions successfully and then fails later at
    guest-agent contact, install, or debug.

    ``crashkernel`` mirrors the local section (the kdump prerequisite token; the
    booted kernel is the arbiter of its grammar). ``destructive_ops`` is the
    deny-by-default opt-in factor for ``force_crash`` (not ``control.power`` or
    ``systems.reprovision``, which are contributor lifecycle).
    ``host_dump`` is the deny-by-default opt-in authorizing the remote host-side dump
    — a ``virsh dump`` of a halted guest's memory to an operator-pool coredump volume,
    streamed to the object store. It mirrors the *policy* of local's
    ``debug.preserve_on_crash`` (per-profile, off by default) but not its mechanism:
    remote takes the dump on demand from a halted System, so this flag adds no
    provisioning-time device — unlike local's pvpanic + ``<on_crash>preserve</on_crash>``.
    There is no SSH credential or gdbstub flag: in-guest access rides the guest-agent
    seam, and the gdbstub is unconditionally enabled with a per-System port the
    provisioning plane allocates.
    """

    base_image_volume: NonEmptyStr | None = None
    base_image_source: LocalComponentRef | None = None
    crashkernel: CrashkernelToken | None = None
    destructive_ops: list[NonEmptyStr] = Field(default_factory=list)
    host_dump: bool = False

    @model_validator(mode="after")
    def _require_exactly_one_base_image(self) -> RemoteLibvirtProfile:
        """Require exactly one of ``base_image_volume`` / ``base_image_source`` (ADR-0440).

        The two are alternatives: an operator-staged volume named by ``base_image_volume``, or a
        supplied worker-host qcow2 in ``base_image_source`` that provisioning stages. Neither is a
        System with no base image; both is an ambiguous source — both are configuration errors.
        """
        present = [self.base_image_volume is not None, self.base_image_source is not None]
        if sum(present) != 1:
            raise ValueError(
                "remote-libvirt requires exactly one of base_image_volume "
                "(an operator-staged volume name) or base_image_source (a supplied local qcow2)"
            )
        return self


# Provenance: ADR-0024 decision 1.
class ProviderSection(_ProfileBase):
    """The provider-specific section, keyed by provider name.

    Exactly one concrete provider section is required. The public properties return the
    concrete section for callers that have already selected a provider-specific path.
    """

    local_libvirt_section: LibvirtProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.LOCAL_LIBVIRT.value,
        serialization_alias=ResourceKind.LOCAL_LIBVIRT.value,
    )
    fault_inject_section: FaultInjectProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.FAULT_INJECT.value,
        serialization_alias=ResourceKind.FAULT_INJECT.value,
    )
    remote_libvirt_section: RemoteLibvirtProfile | None = Field(
        default=None,
        validation_alias=ResourceKind.REMOTE_LIBVIRT.value,
        serialization_alias=ResourceKind.REMOTE_LIBVIRT.value,
    )

    @model_validator(mode="after")
    def _require_exactly_one_provider(self) -> ProviderSection:
        present = [
            self.local_libvirt_section is not None,
            self.fault_inject_section is not None,
            self.remote_libvirt_section is not None,
        ]
        if sum(present) != 1:
            raise ValueError("profile provider must contain exactly one provider section")
        return self

    @property
    def kind(self) -> ResourceKind:
        if self.local_libvirt_section is not None:
            return ResourceKind.LOCAL_LIBVIRT
        if self.remote_libvirt_section is not None:
            return ResourceKind.REMOTE_LIBVIRT
        if self.fault_inject_section is not None:
            return ResourceKind.FAULT_INJECT
        raise AttributeError("profile has no provider section")

    @property
    def destructive_ops(self) -> list[str]:
        if self.local_libvirt_section is not None:
            return list(self.local_libvirt_section.destructive_ops)
        if self.remote_libvirt_section is not None:
            return list(self.remote_libvirt_section.destructive_ops)
        if self.fault_inject_section is not None:
            return list(self.fault_inject_section.destructive_ops)
        raise AttributeError("profile has no provider section")

    @property
    def local_libvirt(self) -> LibvirtProfile:
        if self.local_libvirt_section is None:
            raise AttributeError("profile has no local-libvirt provider section")
        return self.local_libvirt_section

    @property
    def fault_inject(self) -> FaultInjectProfile:
        if self.fault_inject_section is None:
            raise AttributeError("profile has no fault-inject provider section")
        return self.fault_inject_section

    @property
    def remote_libvirt(self) -> RemoteLibvirtProfile:
        if self.remote_libvirt_section is None:
            raise AttributeError("profile has no remote-libvirt provider section")
        return self.remote_libvirt_section


class ProvisioningProfile(_ProfileBase):
    """A versioned provisioning profile: agnostic core plus a provider section.

    The sizing fields (``vcpu`` / ``memory_mb`` / ``disk_gb``) are **optional** (ADR-0024
    delta, ADR-0067): a shape-sized allocation omits them and ``systems.provision``
    constructs them from the resolved sizing snapshot via :func:`reconcile_profile_sizing`
    before the profile is stored. A *present* value is still strictly ``> 0``. A stored
    profile always carries concrete sizing — reconciliation fills the snapshot and the
    no-snapshot lane rejects a NULL-sized profile — so the libvirt renderer never reads a
    ``None`` (it dereferences ``vcpu``/``memory_mb`` unconditionally).
    """

    schema_version: Literal[1]
    arch: NonEmptyStr
    vcpu: int | None = Field(default=None, gt=0, strict=True)
    memory_mb: int | None = Field(default=None, gt=0, strict=True)
    disk_gb: int | None = Field(default=None, gt=0, strict=True)
    boot_method: BootMethod
    kernel_source_ref: NonEmptyStr | None = Field(
        default=None,
        # Provenance: ADR-0078/0080, #472.
        description=(
            "An arbitrary provenance label the operator/agent chooses for the baseline kernel "
            "this System is provisioned against (e.g. 'linux-6.9'), for A/B legibility across "
            "Systems — it is not matched against any warm-tree or inventory list, has no valid-"
            "value set to discover, and is never read by provisioning or job code: any non-empty "
            "string is accepted. Required for boot_method 'direct-kernel' (the System must reach "
            "'ready' on a baseline kernel before its Runs iterate kernels, so the lane needs one "
            "named here); it is an opaque label only, not a URL or fetchable reference. "
            "Omit it for boot_method 'disk-image': that lane boots the operator-staged base "
            "image's own kernel and never reads this field."
        ),
    )
    provider: ProviderSection

    _reject_coerced_version = schema_version_validator

    @model_validator(mode="after")
    def _pair_boot_method_with_provider(self) -> ProvisioningProfile:
        """``disk-image`` and the remote-libvirt section require each other (ADR-0080)."""
        remote = self.provider.remote_libvirt_section is not None
        disk_image = self.boot_method is BootMethod.DISK_IMAGE
        if remote != disk_image:
            raise ValueError(
                "boot_method 'disk-image' and the remote-libvirt provider section "
                "require each other (ADR-0080)"
            )
        return self

    @model_validator(mode="after")
    def _require_ppc64le_and_reservation_for_fadump(self) -> ProvisioningProfile:
        """fadump requires ``arch=ppc64le`` and a ``crashkernel`` reservation (ADR-0349).

        fadump is POWER-specific (the ``ibm,configure-kernel-dump`` RTAS is pseries-only), and in
        kdive's model a crash-capture System is defined by its reservation token — a ``fadump=on``
        with no reservation would resolve to a non-capture method and silently drop the flag. Only
        the local-libvirt section carries the flag; remote/fault-inject have no ``debug`` block.
        """
        section = self.provider.local_libvirt_section
        if section is None or not section.debug.fadump:
            return self
        if self.arch != "ppc64le":
            raise ValueError("debug.fadump is POWER-specific and requires arch 'ppc64le'")
        if section.crashkernel is None:
            raise ValueError("debug.fadump requires a crashkernel reservation")
        return self

    @model_validator(mode="after")
    def _require_fadump_memory_floor(self) -> ProvisioningProfile:
        """fadump requires at least ``FADUMP_MIN_MEMORY_MB`` guest RAM (ADR-0363, #1181).

        fadump reserves a boot-memory region on top of the crashkernel reservation; below the
        floor the guest cannot reach run-readiness (kdump, which reserves only the crashkernel,
        succeeds at the same size). The check fires only on a *concrete* ``memory_mb`` — a
        shape-sized profile omits it and is reconciled to a concrete size before it is stored, so
        the floor is enforced on the size that actually boots (admission re-parses the reconciled
        profile). This is the direct root-cause fix; a slower readiness deadline cannot recover a
        genuine memory shortage.
        """
        section = self.provider.local_libvirt_section
        if section is None or not section.debug.fadump:
            return self
        if self.memory_mb is not None and self.memory_mb < FADUMP_MIN_MEMORY_MB:
            raise ValueError(
                f"debug.fadump requires at least {FADUMP_MIN_MEMORY_MB} MiB of guest memory "
                "(fadump reserves a boot-memory region on top of crashkernel)"
            )
        return self

    @model_validator(mode="after")
    def _require_kernel_source_for_direct_kernel(self) -> ProvisioningProfile:
        """``kernel_source_ref`` is required on ``direct-kernel`` and optional on ``disk-image``.

        A ``disk-image`` provision boots the operator-staged base image's own kernel (ADR-0078/0080)
        and never reads ``kernel_source_ref``, so the VM-only flow must not be forced to supply one
        (#472). The ``direct-kernel`` lane keeps the requirement (the learnable build-iterating
        shape). A present value on ``disk-image`` is accepted and ignored (backward compatible).
        """
        if self.boot_method is BootMethod.DIRECT_KERNEL and self.kernel_source_ref is None:
            raise ValueError("kernel_source_ref is required for boot_method 'direct-kernel'")
        return self

    @classmethod
    def parse(cls, data: ProvisioningProfileInput) -> ProvisioningProfile:
        """Validate a profile document, mapping any failure to ``configuration_error``.

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is
                the caller's responsibility).

        Returns:
            The validated, frozen profile.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure —
                missing/unknown field, wrong type, empty required string, unreadable
                schema version. The error details carry field locations, types, and
                messages, but never the submitted values.
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            details: dict[str, object] = {
                "errors": exc.errors(include_url=False, include_input=False, include_context=False),
            }
            raise CategorizedError(
                "invalid provisioning profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details=details,
            ) from exc


def reconcile_profile_sizing(
    data: ProvisioningProfileInput, sizing: AllocationSizing
) -> dict[str, object]:
    """Build a profile dict whose sizing equals the allocation snapshot (ADR-0024 delta).

    For a shape-sized allocation the resolved tuple is the authority: a profile may omit
    ``vcpu`` / ``memory_mb`` / ``disk_gb`` (they are filled from ``sizing``), or restate
    them — but only with the *same* values; a conflicting restatement is rejected so
    admitted size and booted size can never diverge. Builds a new dict (the immutable
    request-inputs invariant, ADR-0003/0024) rather than mutating the input. Reads only the
    passed snapshot, never the catalog, so a later ``shapes.set`` cannot re-size a stamped
    profile.

    Args:
        data: The submitted profile document (sizing optional or matching).
        sizing: The Allocation's persisted sizing snapshot.

    Returns:
        A new profile dict with concrete ``vcpu`` / ``memory_mb`` / ``disk_gb``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if a submitted size conflicts with the
            snapshot.
    """
    reconciled = dict(data)
    for field, resolved in (
        ("vcpu", sizing.vcpu),
        ("memory_mb", sizing.memory_mb),
        ("disk_gb", sizing.disk_gb),
    ):
        submitted = reconciled.get(field)
        if submitted is not None and submitted != resolved:
            raise CategorizedError(
                f"provisioning profile {field}={submitted!r} conflicts with the "
                f"allocation's resolved size {resolved}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"field": field, "resolved": str(resolved)},
            )
        reconciled[field] = resolved
    return reconciled


def require_concrete_sizing(profile: ProvisioningProfile) -> None:
    """Reject a profile with any NULL sizing field (the no-snapshot lane, ADR-0067).

    A full-custom or legacy allocation carries no resolved sizing snapshot, so its profile
    must supply its own ``vcpu`` / ``memory_mb`` / ``disk_gb``. A stored profile must never
    carry a ``None`` size — the libvirt renderer dereferences them unconditionally.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any sizing field is ``None``.
    """
    missing = [
        field for field in ("vcpu", "memory_mb", "disk_gb") if getattr(profile, field) is None
    ]
    if missing:
        raise CategorizedError(
            f"provisioning profile is missing required sizing: {', '.join(missing)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": missing},
        )


def dump_profile(profile: ProvisioningProfile) -> SerializedProvisioningProfile:
    """Serialize a parsed provisioning profile for JSON persistence."""
    return cast(
        SerializedProvisioningProfile,
        profile.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def profile_digest(profile: ProvisioningProfile) -> str:
    """Return the SHA-256 hex of a canonical encoding of a parsed profile (ADR-0038 §3).

    Computed over the parsed, alias-keyed model dump with sorted keys, so digest equality
    is *semantic* equality: two byte-different but equivalent submissions (key order,
    whitespace) produce the same digest, and any meaningful change produces a distinct one.
    This is the dedup factor in the reprovision ``dedup_key`` (mirrors
    :func:`kdive.security.audit.args_digest`).

    Args:
        profile: A validated profile (parse before hashing — never hash raw input, whose
            ordering and coercions are not normalized).
    """
    canonical = json.dumps(dump_profile(profile), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_rootfs_reference(rootfs: RootfsSource) -> None:
    """Validate a rootfs reference's static resolvability.

    A ``catalog`` reference is checked against the declared ``systems.toml`` inventory — the
    single source of truth for image definitions (ADR-0112), replacing the former packaged
    ``seed_data/`` baseline. A name not declared there but present in the DB catalog (a
    built/published or project-private image) is resolved later by the DB-backed ``materialize``
    fetch, which raises ``CONFIGURATION_ERROR`` for an unknown name; this remains the static,
    connectionless tool-boundary check.

    When no ``systems.toml`` is present (the file is gitignored, so an absent file is the normal
    pre-config state — e.g. a fresh deploy or CI), the static check has no declared baseline to
    consult and accepts the reference, deferring resolution entirely to the DB fetch.
    """
    if not isinstance(rootfs, CatalogComponentRef):
        return
    from kdive.inventory.loader import load_inventory_optional
    from kdive.inventory.path import systems_toml_path

    doc = load_inventory_optional(systems_toml_path())
    if doc is None:
        # No declared baseline (absent file is the normal pre-config state) — defer to the DB
        # fetch, which rejects an unknown name with its own enumeration.
        return
    if any(img.provider == rootfs.provider and img.name == rootfs.name for img in doc.image):
        return
    raise CategorizedError(
        f"unknown rootfs catalog name: {rootfs.name}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "provider": rootfs.provider,
            "name": rootfs.name,
            # The declared (provider, name) set so a black-box caller can self-correct a typo
            # without host access (#731, ADR-0224). Sorted for a stable wire order; only
            # operator-declared catalog identities, never caller input or a secret (no-leak,
            # ADR-0123). Empty only when the declared inventory itself declares no images.
            "available": sorted(f"{img.provider}/{img.name}" for img in doc.image),
        },
    )
