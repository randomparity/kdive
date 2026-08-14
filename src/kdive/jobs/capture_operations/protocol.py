"""Canonical, bounded wire models for supervised capture children (ADR-0558)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.domain.errors import ErrorCategory


def _canonical_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=True)
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


class CaptureRequest(BaseModel):
    """The complete replayable input visible to a capture child after gate release."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    job_id: UUID
    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    resource_id: UUID
    system_id: UUID
    domain_name: Annotated[str, Field(min_length=1, max_length=255)]
    snaplen: Annotated[int, Field(ge=1, le=262_144)]
    max_bytes: Annotated[int, Field(ge=1_048_576, le=536_870_912)]
    max_polls: Annotated[int, Field(ge=1, le=600)]

    def to_canonical_json(self) -> bytes:
        """Serialize the request deterministically for the durable request digest."""
        return _canonical_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a bounded JSON object and reject malformed or non-object input."""
        if len(data) > 16_384:
            raise ValueError("canonical JSON request exceeds 16384 bytes")
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("canonical JSON request is malformed") from error
        if not isinstance(value, dict):
            raise ValueError("canonical JSON request must be an object")
        return cls.model_validate_json(data)

    @property
    def digest(self) -> str:
        """Return the lowercase SHA-256 digest of the canonical request bytes."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


class CaptureResult(BaseModel):
    """Bounded child outcome; arbitrary exception text never crosses this boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outcome: Literal["success", "truncated", "failure"]
    size_bytes: Annotated[int, Field(ge=0, le=536_870_912)]
    truncated: bool
    error_category: ErrorCategory | None = None
    terminal: bool | None = None
    reason: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    details: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_failure(self) -> Self:
        failure = self.outcome == "failure"
        if failure != (self.error_category is not None and self.terminal is not None):
            raise ValueError("failure results require error_category and terminal only on failure")
        if len(json.dumps(self.details, sort_keys=True, separators=(",", ":"))) > 4096:
            raise ValueError("result details exceed 4096 bytes")
        return self

    def to_canonical_json(self) -> bytes:
        """Serialize the result deterministically for the private spool."""
        return _canonical_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a bounded result object and reject malformed JSON."""
        if len(data) > 65_536:
            raise ValueError("result JSON exceeds 65536 bytes")
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("result JSON is malformed") from error
        if not isinstance(value, dict):
            raise ValueError("result JSON must be an object")
        return cls.model_validate_json(data)
