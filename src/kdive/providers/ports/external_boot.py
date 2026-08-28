"""Provider-neutral external Run-boot contracts (ADR-0583)."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Architecture = Literal["x86_64", "ppc64le"]
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type KernelRelease = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"),
]
type CanonicalUuid = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"),
]

_INITRD_MAX_BYTES = 536_870_912
_PLATFORM_ARGUMENT_MAX_BYTES = 256
_CMDLINE_MAX_BYTES = 2_047


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def to_canonical_json(self) -> bytes:
        """Return compact, sorted UTF-8 JSON without a trailing newline."""
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a closed canonical value, rejecting alternate byte encodings."""
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("external-boot value is not canonical JSON")
        return value


def _identity(prefix: bytes, value: _ClosedValue) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + value.to_canonical_json()).hexdigest()


def _nfc(value: str) -> str:
    if not value or unicodedata.normalize("NFC", value) != value:
        raise ValueError("value must be nonempty NFC text")
    return value


class ArtifactSource(_ClosedValue):
    key: Annotated[str, Field(max_length=1024)]
    version: Annotated[str, Field(max_length=1024)]
    sha256: Digest

    _canonical_key = field_validator("key", "version")(_nfc)


class BundleSource(ArtifactSource):
    vmlinuz_sha256: Digest
    member_count: Annotated[int, Field(ge=1, le=200_000)]
    uncompressed_bytes: Annotated[int, Field(ge=1, le=8_589_934_592)]
    vmlinuz_size_bytes: Annotated[int, Field(ge=1, le=536_870_912)]
    decoded_kernel_size_bytes: Annotated[int, Field(ge=1, le=2_147_483_648)]
    elf_metadata_bytes: Annotated[int, Field(ge=1, le=16_777_216)]
    gnu_build_id_size_bytes: Annotated[int, Field(ge=4, le=64)]


class InitrdSource(ArtifactSource):
    size_bytes: Annotated[int, Field(ge=0, le=_INITRD_MAX_BYTES)]


class PlanOwnership(_ClosedValue):
    system_id: CanonicalUuid
    run_id: CanonicalUuid
    build_generation: CanonicalUuid


class ActivationOwnership(_ClosedValue):
    system_id: CanonicalUuid
    run_id: CanonicalUuid


class RootSource(_ClosedValue):
    kind: Literal["staged-image", "catalog-image"]
    identity: Digest


class RootSpecV1(_ClosedValue):
    schema_: Literal["root-spec-v1"] = Field("root-spec-v1", alias="schema")
    architecture: Architecture
    root: Annotated[str, Field(min_length=1, max_length=255)]
    arguments: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    authority: Literal["stage-inspection", "catalog-attestation"]
    source: RootSource

    @field_validator("root")
    @classmethod
    def _root_is_ascii(cls, value: str) -> str:
        if not value.isascii() or any(character.isspace() for character in value):
            raise ValueError("root must be nonempty ASCII without whitespace")
        return value

    @field_validator("arguments")
    @classmethod
    def _arguments_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_platform_argument(value)
        return values

    @model_validator(mode="after")
    def _authority_matches_source(self) -> RootSpecV1:
        expected = {
            "stage-inspection": "staged-image",
            "catalog-attestation": "catalog-image",
        }
        if expected[self.authority] != self.source.kind:
            raise ValueError("root authority/source pairing is invalid")
        if f"root={self.root}" not in self.arguments:
            raise ValueError("root arguments must contain the exact root token")
        return self


class ModuleObligation(_ClosedValue):
    mode: Literal["system-root-tree"] = "system-root-tree"
    release: KernelRelease
    source_manifest: Digest
    member_count: Annotated[int, Field(ge=1, le=200_000)]
    uncompressed_bytes: Annotated[int, Field(ge=0, le=8_589_934_592)]


def _validate_platform_argument(value: str) -> str:
    if not value or not value.isascii() or len(value.encode()) > _PLATFORM_ARGUMENT_MAX_BYTES:
        raise ValueError("platform argument must be 1 through 256 ASCII bytes")
    if "\0" in value or any(character.isspace() for character in value):
        raise ValueError("platform argument must not contain whitespace or NUL")
    if value.startswith("="):
        raise ValueError("platform argument key must be nonempty")
    return value


class ExternalBootPlan(_ClosedValue):
    schema_: Literal["external-boot-plan-v1"] = Field("external-boot-plan-v1", alias="schema")
    architecture: Architecture
    ownership: PlanOwnership
    bundle: BundleSource
    initrd: InitrdSource | None
    cmdline: Annotated[str, Field(min_length=1)]
    debug_cmdline: Annotated[str, Field(min_length=1, max_length=4096)] | None
    platform_arguments: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    module_obligation: ModuleObligation
    root: RootSpecV1

    @field_validator("platform_arguments")
    @classmethod
    def _platform_arguments_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_platform_argument(value)
        return values

    @field_validator("debug_cmdline")
    @classmethod
    def _debug_cmdline_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or "\0" in value or not value.isprintable():
            raise ValueError("debug_cmdline must be stripped printable text")
        if any(token in value for token in ("root=", "console=", "crashkernel=", "fadump=")):
            raise ValueError("debug_cmdline overrides platform arguments")
        return value

    @model_validator(mode="after")
    def _validate_composed_plan(self) -> ExternalBootPlan:
        if self.root.architecture != self.architecture:
            raise ValueError("root architecture must match plan architecture")
        arguments = self.platform_arguments
        root_arguments = self.root.arguments
        occurrences = sum(
            arguments[index : index + len(root_arguments)] == root_arguments
            for index in range(len(arguments) - len(root_arguments) + 1)
        )
        if occurrences != 1:
            raise ValueError("root arguments must occur exactly once in platform arguments")
        keys = [argument.split("=", 1)[0] for argument in arguments]
        if len(keys) != len(set(keys)):
            raise ValueError("platform argument keys must be unique")
        expected_cmdline = " ".join(arguments)
        if self.debug_cmdline is not None:
            expected_cmdline += f" {self.debug_cmdline}"
        if self.cmdline != expected_cmdline:
            raise ValueError("cmdline must exactly compose ordered platform arguments")
        if len(self.cmdline.encode()) > _CMDLINE_MAX_BYTES:
            raise ValueError("cmdline exceeds 2047 UTF-8 bytes")
        return self

    @property
    def identity(self) -> str:
        return _identity(b"kdive-external-boot-plan-v1", self)


class OpaqueProviderRef(_ClosedValue):
    ref: Annotated[str, Field(max_length=1024)]

    @field_validator("ref")
    @classmethod
    def _opaque(cls, value: str) -> str:
        value = _nfc(value)
        if value.startswith(("/", "./", "../")) or "://" in value or "@" in value:
            raise ValueError("provider reference must be opaque")
        return value


class MaterializedArtifacts(_ClosedValue):
    kernel: OpaqueProviderRef
    modules: OpaqueProviderRef
    initrd: OpaqueProviderRef | None


class RunningKernelObservation(_ClosedValue):
    architecture: Architecture
    release: KernelRelease
    gnu_build_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{2}){4,64}$")]


class ExternalBootMaterialization(_ClosedValue):
    schema_: Literal["external-boot-materialization-v1"] = Field(
        "external-boot-materialization-v1", alias="schema"
    )
    architecture: Architecture
    provider_kind: Annotated[str, Field(max_length=255)]
    ownership: ActivationOwnership
    plan_identity: Digest
    extracted_vmlinuz_sha256: Digest
    source_module_manifest: Digest
    installed_module_tree: Digest
    verified_bundle_sha256: Digest
    verified_initrd_sha256: Digest | None
    kernel_observation: RunningKernelObservation
    artifacts: MaterializedArtifacts

    _canonical_provider = field_validator("provider_kind")(_nfc)

    @model_validator(mode="after")
    def _consistent(self) -> ExternalBootMaterialization:
        if self.kernel_observation.architecture != self.architecture:
            raise ValueError("kernel observation architecture must match materialization")
        if (self.artifacts.initrd is None) != (self.verified_initrd_sha256 is None):
            raise ValueError("initrd reference and verified digest must have the same presence")
        return self

    @property
    def identity(self) -> str:
        return _identity(b"kdive-external-boot-materialization-v1", self)


class AbsentComponentState(_ClosedValue):
    state: Literal["absent"] = "absent"


class PresentComponentState(_ClosedValue):
    state: Literal["present"] = "present"
    manifest: Digest


type ComponentState = AbsentComponentState | PresentComponentState


class ProviderStateIdentity(_ClosedValue):
    definition: Digest
    modules: ComponentState


class RecoveryPoint(_ClosedValue):
    schema_: Literal["external-boot-recovery-v1"] = Field(
        "external-boot-recovery-v1", alias="schema"
    )
    ownership: ActivationOwnership
    plan_identity: Digest
    materialization_identity: Digest
    recovery_ref: OpaqueProviderRef
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity


class ExternalBootPorts(Protocol):
    """Six narrow operations shared by external-boot providers."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...

    def prepare(
        self, materialization: ExternalBootMaterialization, authority: OpaqueProviderRef
    ) -> RecoveryPoint: ...

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation: ...

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...
