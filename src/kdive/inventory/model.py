"""Typed pydantic model for the ``systems.toml`` v2 inventory document (ADR-0112).

The model mirrors the ``systems.toml`` schema v2: a list of ``[[image]]`` entries
(each with a discriminated :data:`ImageSource` union), and per-provider instance
lists (``[[remote_libvirt]]`` / ``[[local_libvirt]]`` / ``[[fault_inject]]`` /
``[[build_host]]``).

Parse-time validation enforces three structural invariants:

1. image identity ``(provider, name, arch)`` is unique;
2. instance ``name`` is unique within each provider kind;
3. every instance ``base_image`` cross-reference names a declared ``[[image]]``.

Remote-libvirt is temporarily stricter: only one ``[[remote_libvirt]]`` instance is accepted
until provider operations carry selected Resource identity into remote config resolution.

:meth:`InventoryDoc.parse` is the sanctioned entry point: it wraps
:meth:`~pydantic.BaseModel.model_validate` and re-raises pydantic's structural
``ValidationError`` (e.g. a bad discriminator) as :class:`InventoryError`, so callers
always see one exception type.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, field_validator

from kdive.domain.cost_class_rules import parse_positive_coeff, validate_cost_class_name
from kdive.domain.image_format import ImageFormat
from kdive.domain.models import ImageVisibility
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


ImageSource = Annotated[S3Source | BuildSource | StagedSource, Field(discriminator="kind")]
"""Discriminated union of image realization sources, keyed on the ``kind`` literal."""


class ImageEntry(BaseModel):
    """A single ``[[image]]`` declaration."""

    provider: str
    name: str
    arch: str
    format: ImageFormat
    root_device: str
    visibility: ImageVisibility
    capabilities: list[str] = Field(default_factory=list)
    source: ImageSource

    @property
    def identity(self) -> tuple[str, str, str]:
        """The stable identity tuple ``(provider, name, arch)``."""
        return (self.provider, self.name, self.arch)


class _Instance(BaseModel):
    """Shared fields for a provider instance declaration."""

    name: str
    cost_class: str
    concurrent_allocation_cap: int = 1


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


class LocalLibvirtInstance(_Instance):
    """A ``[[local_libvirt]]`` provider instance."""

    host_uri: str


class FaultInjectInstance(_Instance):
    """A ``[[fault_inject]]`` provider instance."""

    vcpus: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    seed: int = 0


class BuildHostInstance(BaseModel):
    """A ``[[build_host]]`` declaration."""

    name: str
    kind: str
    base_image_volume: str | None = None
    workspace_root: str
    max_concurrent: int = 1


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
    build_host: list[BuildHostInstance] = Field(default_factory=list)
    cost_class: list[CostClassEntry] = Field(default_factory=list)

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
            ("build_host", [i.name for i in self.build_host]),
        )
        for kind, names in groups:
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise InventoryError(kind, "name", f"duplicate instance names {dupes}")

    def _check_remote_libvirt_singleton(self) -> None:
        if len(self.remote_libvirt) <= 1:
            return
        names = sorted(inst.name for inst in self.remote_libvirt)
        raise InventoryError(
            "remote_libvirt",
            "instances",
            "multiple instances are not supported until per-op remote resource selection is wired "
            f"{names}",
        )

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
            raise InventoryError("inventory", "schema", str(exc)) from exc
        doc._check_image_identities()
        doc._check_base_image_refs()
        doc._check_instance_name_uniqueness()
        doc._check_remote_libvirt_singleton()
        doc._check_cost_class_uniqueness()
        return doc
