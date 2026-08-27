"""Bounded request and response models for host worker lifecycle operations (ADR-0574)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

Operation = Literal["start", "status", "stop", "diagnostics"]
LIFECYCLE_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 1_114_112
MAX_STRING_BYTES = 4096
ResponseCode = Literal[
    "ok",
    "busy",
    "invalid_request",
    "deadline_exceeded",
    "conflict",
    "dependency_unavailable",
    "evidence_rejected",
    "diagnostics_withheld",
    "internal_error",
]
RetryAction = Literal[
    "none",
    "correct_request",
    "retry_same_operation",
    "restore_systemd",
    "restore_database",
    "operator_recovery",
]


def _protocol_identity(
    version: int, request_schema: dict[str, Any], response_schema: dict[str, Any]
) -> str:
    """Bind semantic protocol version and structural wire schemas into one identity."""
    canonical = json.dumps(
        {"request": request_schema, "response": response_schema},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"{version}:{hashlib.sha256(canonical).hexdigest()}"


def validate_utf8_bytes(value: str, maximum: int) -> str:
    """Return one string only when its UTF-8 representation fits its protocol limit."""
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"string exceeds {maximum} UTF-8 bytes")
    return value


class SlotPhase(StrEnum):
    """Durable phases for an exact systemd worker incarnation."""

    PREPARED = "prepared"
    GATED = "gated"
    REGISTERED = "registered"
    STARTED = "started"
    TERMINATED = "terminated"


class WorkerSettings(BaseModel):
    """The complete, caller-provided allowlist for one fixed worker fleet."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=4096)

    python: str
    source_root: str
    rootfs_dir: str
    build_workspace: str
    build_component_roots: str
    install_staging: str
    fixture_catalog_path: str
    worker_database_url: SecretStr
    libvirt_uri: str
    s3_endpoint_url: str
    s3_bucket: str
    s3_region: str
    aws_access_key_id: SecretStr
    aws_secret_access_key: SecretStr
    accepted_lanes: tuple[Literal["default", "state-fenced"], ...] = Field(
        min_length=1, max_length=2
    )
    build_user: str
    log_level: str
    health_binds: dict[int, str] = Field(max_length=8)

    @field_validator(
        "python",
        "source_root",
        "rootfs_dir",
        "build_workspace",
        "build_component_roots",
        "install_staging",
        "fixture_catalog_path",
        "libvirt_uri",
        "s3_endpoint_url",
        "s3_bucket",
        "s3_region",
        "build_user",
        "log_level",
    )
    @classmethod
    def validate_string_bytes(cls, value: str) -> str:
        """Apply the 4-KiB bound in wire bytes, not Unicode code points."""
        return validate_utf8_bytes(value, MAX_STRING_BYTES)

    @field_validator("worker_database_url", "aws_access_key_id", "aws_secret_access_key")
    @classmethod
    def validate_secret_bytes(cls, value: SecretStr) -> SecretStr:
        """Apply the 4-KiB wire bound without exposing a secret in validation errors."""
        validate_utf8_bytes(value.get_secret_value(), MAX_STRING_BYTES)
        return value

    @field_validator(
        "python",
        "source_root",
        "rootfs_dir",
        "build_workspace",
        "build_component_roots",
        "install_staging",
        "fixture_catalog_path",
    )
    @classmethod
    def validate_absolute_path(cls, value: str) -> str:
        """Allow only absolute paths to cross the lifecycle control boundary."""
        if not value.startswith("/"):
            raise ValueError("worker paths must be absolute")
        return value

    @field_validator("health_binds")
    @classmethod
    def validate_health_slots(cls, value: dict[int, str]) -> dict[int, str]:
        """Keep health binds assigned to the fixed worker slot range."""
        if any(slot not in range(1, 9) for slot in value):
            raise ValueError("health binds must use slots 1 through 8")
        for bind in value.values():
            validate_utf8_bytes(bind, MAX_STRING_BYTES)
        return value

    @field_validator("accepted_lanes")
    @classmethod
    def validate_unique_lanes(
        cls, value: tuple[Literal["default", "state-fenced"], ...]
    ) -> tuple[Literal["default", "state-fenced"], ...]:
        """Reject repeated lanes rather than minting duplicate worker claim loops."""
        if len(value) != len(set(value)):
            raise ValueError("accepted lanes must be unique")
        return value


class LifecycleRequest(BaseModel):
    """One complete lifecycle request, with configuration accepted only for ``start``."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=4096)

    operation: Operation
    worker_count: int | None = Field(default=None, ge=1, le=8)
    settings: WorkerSettings | None = None

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        """Decode one request only when its complete UTF-8 frame fits the protocol ceiling."""
        if not isinstance(json_data, bytes):
            raise TypeError("lifecycle request frame must be bytes")
        if len(json_data) > MAX_REQUEST_BYTES:
            raise ValueError("lifecycle request exceeds 32 KiB")
        return super().model_validate_json(json_data, **kwargs)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        """Reject configuration on non-start requests and incomplete starts."""
        has_start_fields = self.worker_count is not None or self.settings is not None
        if (self.operation == "start") != has_start_fields:
            raise ValueError("only start requires worker_count and settings")
        if self.operation == "start" and (self.worker_count is None or self.settings is None):
            raise ValueError("start requires worker_count and settings")
        return self

    def to_wire_bytes(self) -> bytes:
        """Serialize secrets only for the peer-authenticated local control wire."""
        payload = self.model_dump(mode="json")
        if self.settings is not None:
            settings = self.settings.model_dump(mode="json")
            for name in ("worker_database_url", "aws_access_key_id", "aws_secret_access_key"):
                secret = getattr(self.settings, name)
                settings[name] = secret.get_secret_value()
            payload["settings"] = settings
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class SlotResult(BaseModel):
    """A bounded result for one fixed systemd worker slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: int = Field(ge=1, le=8)
    unit: Annotated[str, StringConstraints(max_length=128)]
    phase: SlotPhase | None = None
    code: Annotated[str, StringConstraints(max_length=64)] = "ok"
    message: Annotated[str, StringConstraints(max_length=1024)] = ""

    @field_validator("unit", "code", "message")
    @classmethod
    def validate_string_bytes(cls, value: str, info: ValidationInfo) -> str:
        """Keep each result field within its documented UTF-8 wire budget."""
        limits = {"unit": 128, "code": 64, "message": 1024}
        field = info.field_name
        if field is None:
            raise RuntimeError("result byte validator has no field name")
        return validate_utf8_bytes(value, limits[field])

    @model_validator(mode="after")
    def validate_fixed_unit(self) -> Self:
        """Keep response results bound to their fixed derived unit names."""
        if self.unit != f"kdive-live-worker@{self.slot}.service":
            raise ValueError("slot result must use its derived systemd unit")
        return self


class LifecycleResponse(BaseModel):
    """The complete bounded server response frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    code: ResponseCode
    message: Annotated[str, StringConstraints(max_length=4096)]
    retry_action: RetryAction
    slots: tuple[SlotResult, ...] = Field(max_length=8, default=())
    diagnostics: Annotated[str, StringConstraints(max_length=1_048_576)] | None = None

    @field_validator("message")
    @classmethod
    def validate_message_bytes(cls, value: str) -> str:
        """Keep the response message within its 4-KiB UTF-8 bound."""
        return validate_utf8_bytes(value, MAX_STRING_BYTES)

    @field_validator("diagnostics")
    @classmethod
    def validate_diagnostics_bytes(cls, value: str | None) -> str | None:
        """Keep diagnostics within its 1-MiB UTF-8 bound before framing."""
        if value is not None:
            validate_utf8_bytes(value, 1_048_576)
        return value

    @model_validator(mode="after")
    def validate_ordered_slots(self) -> Self:
        """Reject duplicate or unordered results rather than silently reordering evidence."""
        slots = tuple(result.slot for result in self.slots)
        if slots != tuple(sorted(set(slots))):
            raise ValueError("slot results must be ordered and unique")
        return self

    def to_json_bytes(self) -> bytes:
        """Serialize one server response only when its UTF-8 frame fits the wire ceiling."""
        frame = self.model_dump_json().encode("utf-8")
        if len(frame) > MAX_RESPONSE_BYTES:
            raise ValueError("lifecycle response exceeds 1,114,112 bytes")
        return frame


def lifecycle_protocol_identity() -> str:
    """Return the semantic and structural identity of the local lifecycle protocol."""
    return _protocol_identity(
        LIFECYCLE_PROTOCOL_VERSION,
        LifecycleRequest.model_json_schema(),
        LifecycleResponse.model_json_schema(),
    )


def client_exit_status(response: LifecycleResponse) -> int:
    """Map a validated response to the lifecycle client's fail-closed exit status."""
    if (response.code == "ok") != response.ok:
        return 5
    if response.ok:
        return 0
    if response.code == "invalid_request":
        return 2
    if response.code == "busy":
        return 3
    return 4
