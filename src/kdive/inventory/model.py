"""Typed pydantic model for the ``systems.toml`` v2 inventory document (ADR-0112).

The model mirrors the ``systems.toml`` schema v2: a list of ``[[image]]`` entries
(each with a discriminated :data:`ImageSource` union), and per-provider instance
lists (``[[remote_libvirt]]`` / ``[[local_libvirt]]`` / ``[[fault_inject]]``).

Parse-time validation enforces three structural invariants:

1. image identity ``(provider, name, arch)`` is unique;
2. instance ``name`` is unique within each provider kind;
3. every instance ``base_image`` cross-reference names a declared ``[[image]]``.

Remote-libvirt accepts multiple named ``[[remote_libvirt]]`` instances. Reconcile creates one
config-owned resource per instance, and per-op provider resolution selects the instance config by
the granted resource's ``name`` (ADR-0187).

:meth:`InventoryDoc.parse` is the sanctioned entry point: it wraps
:meth:`~pydantic.BaseModel.model_validate` and re-raises pydantic's structural
``ValidationError`` (e.g. a bad discriminator) as :class:`InventoryError`, so callers
always see one exception type.
"""

from __future__ import annotations

from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from kdive.domain.accounting.cost_class_rules import parse_positive_coeff, validate_cost_class_name
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import Capability, ImageVisibility
from kdive.images.planes.base import (
    PROVENANCE_BOOT_KERNEL_COUNT,
    PROVENANCE_MAKEDUMPFILE_VERSION,
)
from kdive.inventory.errors import InventoryError


class S3Source(BaseModel):
    """An image realized from an object in the S3-compatible store."""

    kind: Literal["s3"]
    object_key: str
    digest: str | None = None
    """Required to reach ``registered``; a HEAD only confirms object existence."""


class BuildSource(BaseModel):
    """An image built in-tree from a base plus optional build components."""

    kind: Literal["build"]
    base: str
    components: list[str] = Field(default_factory=list)


class StagedSource(BaseModel):
    """An image backed by an operator-staged provider volume (no S3 object)."""

    kind: Literal["staged"]
    volume: str


class StagedPathSource(BaseModel):
    """An image backed by an operator-staged rootfs file under a local-libvirt provider root.

    The local-libvirt analog of :class:`StagedSource` (which names a libvirt storage-pool volume):
    ``path`` is an absolute host path validated against the provider's ``allowed_roots`` at
    provision time. No S3 object, no digest (ADR-0228). Public-only — see :class:`ImageEntry`.
    """

    kind: Literal["staged-path"]
    path: str

    @field_validator("path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("staged-path source path must be absolute")
        return value


ImageSource = Annotated[
    S3Source | BuildSource | StagedSource | StagedPathSource, Field(discriminator="kind")
]
"""Discriminated union of image realization sources, keyed on the ``kind`` literal."""


# Operator ``description`` is echoed on every ``images.list`` row (ADR-0311), so it is capped to
# a one-line hint for token safety — well under the worker's 1000-char value cap.
_MAX_IMAGE_DESCRIPTION = 280


class AttestedProvenance(BaseModel):
    """Operator-attested capability operands for an externally-baked ``s3`` image (ADR-0323).

    An operator who bakes an ``s3`` catalog image outside KDIVE can attest the two registered
    capability-signal operands here; the reconciler synthesizes them into the row's ``provenance``
    and marks it operator-attested (``image_catalog.provenance_attested``), so the pre-provision
    ``direct_kernel``/``kdump`` check is actionable without a KDIVE build. These are operator
    *claims*, not verified facts — ``images.describe`` labels a signal computed from them
    ``basis = "operator_attested"``. Both fields are optional so an operator can attest just one;
    :meth:`as_provenance` omits an unset operand entirely.
    """

    boot_kernel_count: int | None = Field(default=None, ge=0)
    makedumpfile_version: str | None = None

    def as_provenance(self) -> dict[str, object]:
        """The declared operands as a provenance dict, omitting any unset operand."""
        prov: dict[str, object] = {}
        if self.boot_kernel_count is not None:
            prov[PROVENANCE_BOOT_KERNEL_COUNT] = self.boot_kernel_count
        if self.makedumpfile_version:
            prov[PROVENANCE_MAKEDUMPFILE_VERSION] = self.makedumpfile_version
        return prov

    def is_empty(self) -> bool:
        """True when no operand is attested (an empty ``[image.attested]`` table)."""
        return not self.as_provenance()


class ImageEntry(BaseModel):
    """A single ``[[image]]`` declaration."""

    provider: str
    name: str
    arch: str
    format: ImageFormat
    root_device: str
    visibility: ImageVisibility
    capabilities: list[Capability] = Field(default_factory=list)
    source: ImageSource
    description: str = ""
    attested: AttestedProvenance | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        """The stable identity tuple ``(provider, name, arch)``."""
        return (self.provider, self.name, self.arch)

    @model_validator(mode="after")
    def _attested_only_on_s3(self) -> Self:
        """Restrict ``[image.attested]`` to ``s3`` sources with at least one operand (ADR-0323).

        Only an externally-baked ``s3`` image lacks build-recorded operands and cannot carry a
        build-fs sidecar, so attestation is the sole way to characterize it. A ``build`` source owns
        publish-verified provenance and a ``staged-path`` source has its sidecar; attesting either
        would let an operator claim shadow a verified fact, so it is rejected at load. An empty
        table is a config mistake (attests nothing) and is likewise rejected.
        """
        if self.attested is None:
            return self
        if not isinstance(self.source, S3Source):
            raise ValueError(
                f"image {self.name!r} declares [image.attested] on a "
                f"{self.source.kind!r} source; attestation is only supported for 's3' images"
            )
        if self.attested.is_empty():
            raise ValueError(
                f"image {self.name!r} declares an empty [image.attested] table; attest at least "
                "one of boot_kernel_count / makedumpfile_version, or remove the table"
            )
        return self

    @model_validator(mode="after")
    def _description_within_cap(self) -> Self:
        """Reject an over-long operator description, naming the image and the limit (ADR-0311).

        The hint is surfaced on every ``images.list`` row, so an unbounded value would multiply
        across a page and blow an agent's context budget. The cap is enforced at load, not silently
        truncated, so the operator sees the problem.
        """
        if len(self.description) > _MAX_IMAGE_DESCRIPTION:
            raise ValueError(
                f"image {self.name!r} description exceeds {_MAX_IMAGE_DESCRIPTION} characters "
                f"({len(self.description)}); keep it to a one-line operator hint"
            )
        return self

    @model_validator(mode="after")
    def _staged_path_is_public(self) -> Self:
        """A ``staged-path`` image must be public (ADR-0228).

        The local-libvirt catalog rootfs lane resolves at public scope, so a private staged-path
        row would surface to its owning project via ``images.list`` yet be unresolvable at provision
        — a discoverable-but-unprovisionable trap. Reject it at load instead.
        """
        if isinstance(self.source, StagedPathSource) and self.visibility != ImageVisibility.PUBLIC:
            raise ValueError(
                "a staged-path image must have visibility = 'public'; project-private local "
                "staged-path images are not supported"
            )
        return self


class _Instance(BaseModel):
    """Shared fields for a provider instance declaration."""

    name: str
    cost_class: str
    concurrent_allocation_cap: int = 1
    # The pool label written to resources.pool; groups interchangeable hosts for first-available
    # by-pool allocation (ADR-0186). Absent → 'default'.
    pool: str = "default"


class RemoteLibvirtInstance(_Instance):
    """A ``[[remote_libvirt]]`` provider instance.

    ``vcpus`` / ``memory_mb`` are the host's billable size ceiling: admission's
    ≤-resource-caps check (ADR-0007 §2) reads them off the Resource, so a remote host with
    no declared ceiling is un-grantable (``configuration_error``). Unlike local-libvirt —
    whose ceiling is probed by discovery — remote-libvirt is config-owned, so the file is the
    only place the ceiling can come from and both are required.
    """

    uri: str
    gdb_addr: str
    gdbstub_range: str
    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str
    base_image: str
    vcpus: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    shapes: list[str] = Field(default_factory=list)
    # Optional SSH forward (ADR-0291): both set → provisioning renders a per-System user-mode
    # hostfwd on ``ssh_addr`` (the ACL'd bind address, sibling of ``gdb_addr``) and the bootstrap
    # key is injected, so ``ssh_info``/``authorize_ssh_key`` work on remote Systems. Both unset
    # keeps guest-agent-only behavior; exactly one set is a config error.
    ssh_addr: str | None = None
    ssh_range: str | None = None
    authority_instance: str | None = None
    authority_address: str | None = None
    authority_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    authority_server_ca_ref: str | None = None
    authority_client_cert_ref: str | None = None
    authority_client_key_ref: str | None = None
    authority_store_identity: str | None = Field(default=None, min_length=1, max_length=1024)
    authority_recovery_reserve_bytes: int | None = Field(default=None, gt=0, strict=True)
    authority_recovery_max_bytes: int | None = Field(default=None, gt=0, strict=True)

    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "authority_instance",
        "authority_server_ca_ref",
        "authority_client_cert_ref",
        "authority_client_key_ref",
        "authority_store_identity",
    )
    @classmethod
    def _authority_text_is_non_empty(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty")
        return value

    @field_validator("authority_address")
    @classmethod
    def _authority_address_is_canonical_ipv4(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = ip_address(value)
        except ValueError as exc:
            raise ValueError("authority_address must be a canonical numeric IPv4 address") from exc
        if isinstance(parsed, IPv6Address):
            raise ValueError("authority_address must be IPv4; IPv6 is unsupported")
        assert isinstance(parsed, IPv4Address)
        if str(parsed) != value:
            raise ValueError("authority_address must be a canonical numeric IPv4 address")
        if parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("authority_address must not be unspecified or multicast")
        return value

    @model_validator(mode="after")
    def _authority_binding_is_complete(self) -> Self:
        values = (
            self.authority_instance,
            self.authority_address,
            self.authority_port,
            self.authority_server_ca_ref,
            self.authority_client_cert_ref,
            self.authority_client_key_ref,
            self.authority_store_identity,
            self.authority_recovery_reserve_bytes,
            self.authority_recovery_max_bytes,
        )
        if all(value is None for value in values):
            return self
        if any(value is None for value in values):
            raise ValueError("authority binding fields must be all present or all absent")
        refs = {
            self.authority_server_ca_ref,
            self.authority_client_cert_ref,
            self.authority_client_key_ref,
        }
        if len(refs) != 3:
            raise ValueError("authority secret references must be distinct")
        assert self.authority_recovery_reserve_bytes is not None
        assert self.authority_recovery_max_bytes is not None
        if self.authority_recovery_reserve_bytes > self.authority_recovery_max_bytes:
            raise ValueError("authority recovery reserve bytes must not exceed maximum bytes")
        return self


class LocalLibvirtInstance(_Instance):
    """A ``[[local_libvirt]]`` provider instance."""

    host_uri: str
    # Operator opt-in to guest outbound egress (#1031, ADR-0313). Default ``False`` keeps the
    # SSH-forward NIC's ``restrict=on`` (no egress); ``True`` renders ``restrict=off`` so the guest
    # gets SLIRP NAT + DNS and an agent can install tools at runtime. Operator-owned: it is resolved
    # from this file at provision time, never from the allocation/provision request.
    guest_egress: bool = False
    # Opt-in for activation-free provisioning on the matching authority host. The server records
    # only this identity; socket paths and credentials remain fixed worker/host configuration.
    authority_instance: str | None = Field(default=None, min_length=1, max_length=255)
    # Operator declaration for the live-vm fixture lane (ADR-0752): Systems provisioned on this
    # Resource by the fixed worker are moved onto the named authority's daemon afterwards, so Runs
    # may use that authority. Provisioning is unchanged; exclusive with ``authority_instance``.
    relocated_authority_instance: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("authority_instance", "relocated_authority_instance")
    @classmethod
    def _authority_instance_is_nonblank(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty")
        return value

    @model_validator(mode="after")
    def _route_and_relocation_are_exclusive(self) -> Self:
        if self.authority_instance is not None and self.relocated_authority_instance is not None:
            raise ValueError(
                "authority_instance and relocated_authority_instance are mutually exclusive"
            )
        return self


class FaultInjectInstance(_Instance):
    """A ``[[fault_inject]]`` provider instance."""

    vcpus: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    seed: int = 0


class ByoHostOob(BaseModel):
    """Refs-only service-processor coordinates; no driver is enabled here (ADR-0813)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    kind: Literal["redfish", "ipmi", "hmc"]
    endpoint: str
    username_ref: str
    password_ref: str
    managed_system: str | None = None
    lpar_name: str | None = None

    @field_validator("endpoint", "username_ref", "password_ref", "managed_system", "lpar_name")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must be nonblank")
        return value

    @field_validator("endpoint")
    @classmethod
    def _no_inline_credentials(cls, value: str) -> str:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if parsed.username is not None:
            raise ValueError("endpoint must not contain user-info; use credential refs")
        return value

    @model_validator(mode="after")
    def _partition_coordinates(self) -> Self:
        if self.kind == "hmc":
            if self.managed_system is None or self.lpar_name is None:
                raise ValueError("hmc requires managed_system and lpar_name")
        elif self.managed_system is not None or self.lpar_name is not None:
            raise ValueError("managed_system and lpar_name apply only to hmc")
        return self


class ByoHostInstance(_Instance):
    """One declared x86 host or PowerVM LPAR; operations remain unavailable (ADR-0813)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    arch: Literal["x86_64", "ppc64le"]
    ssh_target: str
    ssh_key_ref: str
    oob: ByoHostOob
    baseline_kernel: str
    console_device: str
    vcpus: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    cost_class: str = "byo"
    concurrent_allocation_cap: int = Field(default=1, ge=1, le=1, strict=True)
    pseries_fadump: bool = Field(default=False, strict=True)

    @field_validator(
        "name", "ssh_target", "ssh_key_ref", "baseline_kernel", "console_device", "pool"
    )
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be nonblank")
        return value

    @field_validator("cost_class")
    @classmethod
    def _cost_class_name(cls, value: str) -> str:
        return validate_cost_class_name(value)

    @field_validator("ssh_target")
    @classmethod
    def _no_ssh_password(cls, value: str) -> str:
        if "://" in value:
            password = urlsplit(value).password
        else:
            password = (
                value.split("@", 1)[0] if "@" in value and ":" in value.split("@", 1)[0] else None
            )
        if password is not None:
            raise ValueError("ssh_target must not contain a password; use ssh_key_ref")
        return value

    @model_validator(mode="after")
    def _platform_matches_oob(self) -> Self:
        if (self.arch == "ppc64le") != (self.oob.kind == "hmc"):
            raise ValueError("ppc64le requires hmc; x86_64 requires redfish or ipmi")
        if self.arch != "ppc64le" and self.pseries_fadump:
            raise ValueError("pseries_fadump applies only to ppc64le")
        return self


class CostClassEntry(BaseModel):
    """A single ``[[cost_class]]`` declaration: a pricing coefficient for a cost class.

    Validation delegates to ``domain/cost_class_rules`` — the same rule
    ``ops.set_cost_class_coeff`` applies — so the file and the tool cannot diverge. A
    field-validator raising ``ValueError`` surfaces as a pydantic ``ValidationError`` that
    :meth:`InventoryDoc.parse` maps to :class:`InventoryError` (ADR-0115 §1, §6).
    """

    name: str
    coeff: Decimal

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return validate_cost_class_name(value)

    @field_validator("coeff", mode="before")
    @classmethod
    def _check_coeff(cls, value: object) -> Decimal:
        return parse_positive_coeff(value)


class InventoryDoc(BaseModel):
    """The parsed ``systems.toml`` v2 document."""

    schema_version: Literal[2]
    image: list[ImageEntry] = Field(default_factory=list)
    remote_libvirt: list[RemoteLibvirtInstance] = Field(default_factory=list)
    local_libvirt: list[LocalLibvirtInstance] = Field(default_factory=list)
    fault_inject: list[FaultInjectInstance] = Field(default_factory=list)
    cost_class: list[CostClassEntry] = Field(default_factory=list)
    byo_host: list[ByoHostInstance] = Field(default_factory=list)

    def _check_image_identities(self) -> None:
        seen: set[tuple[str, str, str]] = set()
        for img in self.image:
            if img.identity in seen:
                raise InventoryError(
                    f"image[{img.name}]",
                    "identity",
                    f"duplicate (provider,name,arch) {img.identity}",
                )
            seen.add(img.identity)

    def _check_base_image_refs(self) -> None:
        declared = {img.name for img in self.image}
        for inst in self.remote_libvirt:
            if inst.base_image not in declared:
                raise InventoryError(
                    f"remote_libvirt[{inst.name}]",
                    "base_image",
                    f"names undeclared image {inst.base_image!r}",
                )

    def _check_instance_name_uniqueness(self) -> None:
        groups: tuple[tuple[str, list[str]], ...] = (
            ("remote_libvirt", [i.name for i in self.remote_libvirt]),
            ("local_libvirt", [i.name for i in self.local_libvirt]),
            ("fault_inject", [i.name for i in self.fault_inject]),
            ("byo_host", [i.name for i in self.byo_host]),
        )
        for kind, names in groups:
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise InventoryError(kind, "name", f"duplicate instance names {dupes}")

    def _check_byo_coordinates(self) -> None:
        ssh: set[str] = set()
        bmc: set[str] = set()
        hmc: set[tuple[str, str | None, str | None]] = set()
        for index, inst in enumerate(self.byo_host):
            entry = f"byo_host[{index}]"
            if inst.ssh_target in ssh:
                raise InventoryError(entry, "ssh_target", "duplicate SSH target")
            ssh.add(inst.ssh_target)
            oob = inst.oob
            if oob.kind == "hmc":
                identity = (oob.endpoint, oob.managed_system, oob.lpar_name)
                if identity in hmc:
                    raise InventoryError(entry, "oob", "duplicate HMC partition identity")
                hmc.add(identity)
            else:
                if oob.endpoint in bmc:
                    raise InventoryError(entry, "oob.endpoint", "duplicate BMC endpoint")
                bmc.add(oob.endpoint)

    def _check_cost_class_uniqueness(self) -> None:
        names = [c.name for c in self.cost_class]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise InventoryError("cost_class", "name", f"duplicate cost_class names {dupes}")

    @classmethod
    def parse(cls, data: dict[str, Any]) -> Self:
        """Validate ``data`` into an :class:`InventoryDoc`.

        First runs pydantic structural validation, re-raising pydantic's
        ``ValidationError`` (e.g. an unknown source discriminator, a missing
        required field, or a bad ``schema_version``) as :class:`InventoryError`.
        Then runs the semantic checks (image-identity uniqueness, ``base_image``
        cross-reference, per-kind instance-name uniqueness) directly, so their
        :class:`InventoryError` propagates with its precise ``entry``/``field``
        intact rather than being flattened by a pydantic after-validator.

        Either way the caller observes exactly one exception type.

        Args:
            data: The decoded TOML mapping.

        Returns:
            The validated document.

        Raises:
            InventoryError: On any structural or semantic validation failure.
        """
        try:
            doc = cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors(include_input=False)[0]
            location = first["loc"]
            if location and location[0] == "byo_host":
                entry = f"byo_host[{location[1]}]" if len(location) > 1 else "byo_host"
                field = ".".join(str(part) for part in location[2:]) or "schema"
                raise InventoryError(entry, field, first["msg"]) from None
            raise InventoryError("inventory", "schema", str(exc)) from exc
        doc._check_image_identities()
        doc._check_base_image_refs()
        doc._check_instance_name_uniqueness()
        doc._check_cost_class_uniqueness()
        doc._check_byo_coordinates()
        return doc
