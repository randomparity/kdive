"""Strict version-1 qualification inputs; no submitted value becomes a diagnostic."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

Version = Annotated[int, Field(strict=True, ge=1, le=1)]
GitSHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:-]*$", max_length=512)]
NodeID = Annotated[
    str,
    Field(
        pattern=(
            r"^tests/[a-zA-Z0-9_/]+\.py::[a-zA-Z_][a-zA-Z0-9_]*"
            r"(?:::[a-zA-Z_][a-zA-Z0-9_]*)*(?:\[[^\x00-\x1f\x7f]*\])?$"
        ),
        max_length=512,
    ),
]
OSIdentity = Annotated[
    str,
    Field(
        pattern=(
            r"^(ubuntu|debian|fedora|rhel|rocky|almalinux|centos-stream|"
            r"opensuse-leap|opensuse-tumbleweed|sles):[0-9]+(?:\.[0-9]+)*$"
        ),
        max_length=64,
    ),
]
Architecture = Literal["x86_64", "ppc64le", "aarch64"]
Role = Literal["server", "worker", "reconciler", "authority"]
_MAX_BYTES = 64 * 1024 * 1024
_MAX_RECORDS = 100_000


class Outcome(StrEnum):
    SUCCESS = "success"
    REJECTION = "rejection"
    UNSUPPORTED = "unsupported"
    FAILURE = "failure"
    BLOCKED = "blocked"
    NOT_RUN = "not-run"


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host_os: OSIdentity
    host_arch: Architecture
    guest_os: OSIdentity | None = None
    guest_arch: Architecture | None = None
    accelerator: Literal["none", "kvm", "kvm-hv", "tcg"]
    image_sha256: SHA256 | None = None
    kernel_sha256: SHA256 | None = None
    kernel_source_sha: GitSHA | None = None
    kernel_config_sha256: SHA256 | None = None
    compiler_id: SHA256 | None = None
    kernel_build_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16,128}$")] | None = None


class InputBindings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Version
    candidate_sha: GitSHA
    matrix_sha256: SHA256
    cells: dict[Identifier, Context] = Field(max_length=_MAX_RECORDS)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Version
    cell_id: Identifier
    scenario_id: Identifier
    node_id: NodeID
    outcome: Outcome
    candidate_sha: GitSHA
    matrix_sha256: SHA256
    input_sha256: SHA256
    deployed_roles: dict[Role, GitSHA]
    context: Context
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    assertions: dict[Identifier, SHA256] = Field(max_length=256)
    artifacts: list[SHA256] = Field(max_length=256)
    impediments: list[Literal["known-defect", "missing-prerequisite"]] = Field(max_length=2)

    @field_validator("outcome", mode="before")
    @classmethod
    def parse_outcome(cls, value: object) -> Outcome:
        if not isinstance(value, (str, Outcome)):
            raise ValueError("invalid outcome")
        return Outcome(value)


class EvidenceError(ValueError):
    """Only closed categories and schema-owned field paths may enter this message."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("invalid-json: duplicate object key; keep one value per key")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise EvidenceError("invalid-json: size limit exceeded; split producer diagnostics out")
        return json.loads(raw, object_pairs_hook=_unique_object)
    except OSError:
        raise EvidenceError("unreadable-input: check the supplied file and permissions") from None
    except (ValueError, RecursionError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError(
            "invalid-json: supply a UTF-8 JSON document within nesting limits"
        ) from None


def _validation_error(error: ValidationError) -> EvidenceError:
    # Dict keys and unknown field names in loc are attacker-controlled, including extra fields.
    known = set(Context.model_fields) | set(InputBindings.model_fields) | set(Evidence.model_fields)
    location = error.errors(include_input=False, include_context=False)[0]["loc"]
    path = (
        ".".join(
            str(part) if isinstance(part, int) else part if part in known else "*"
            for part in location
        )
        or "root"
    )
    return EvidenceError(f"invalid-evidence at {path}: correct the version-1 field shape")


def read_bindings(path: Path) -> InputBindings:
    try:
        return InputBindings.model_validate(_read_json(path))
    except ValidationError as error:
        raise _validation_error(error) from None


def read_results(path: Path) -> list[Evidence]:
    try:
        return TypeAdapter(
            Annotated[list[Evidence], Field(max_length=_MAX_RECORDS)]
        ).validate_python(
            _read_json(path),
            strict=True,
        )
    except ValidationError as error:
        raise _validation_error(error) from None
