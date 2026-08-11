"""Bounded request and response models for host worker lifecycle operations."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

Operation = Literal["start", "status", "stop", "diagnostics"]
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
    accepted_lanes: tuple[Literal["default", "state-fenced"], ...] = Field(min_length=1)
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
        return value


class LifecycleRequest(BaseModel):
    """One complete lifecycle request, with configuration accepted only for ``start``."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=4096)

    operation: Operation
    worker_count: int | None = Field(default=None, ge=1, le=8)
    settings: WorkerSettings | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        """Reject configuration on non-start requests and incomplete starts."""
        has_start_fields = self.worker_count is not None or self.settings is not None
        if (self.operation == "start") != has_start_fields:
            raise ValueError("only start requires worker_count and settings")
        if self.operation == "start" and (self.worker_count is None or self.settings is None):
            raise ValueError("start requires worker_count and settings")
        return self


class SlotResult(BaseModel):
    """A bounded result for one fixed systemd worker slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: int = Field(ge=1, le=8)
    unit: Annotated[str, StringConstraints(max_length=128)]
    phase: SlotPhase | None = None
    code: Annotated[str, StringConstraints(max_length=64)] = "ok"
    message: Annotated[str, StringConstraints(max_length=1024)] = ""

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

    @model_validator(mode="after")
    def validate_ordered_slots(self) -> Self:
        """Reject duplicate or unordered results rather than silently reordering evidence."""
        slots = tuple(result.slot for result in self.slots)
        if slots != tuple(sorted(set(slots))):
            raise ValueError("slot results must be ordered and unique")
        return self


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
