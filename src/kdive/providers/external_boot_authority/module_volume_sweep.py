"""Closed wire values for the authority-side module-volume sweep (ADR-0588, ADR-0603).

The worker names the module attempts whose volumes must be kept; the authority sweeps its own
pool and reports how many volumes it removed. The retained set is bounded and never truncated:
a set over the bound is refused rather than reduced, because deleting around a truncated set
would destroy a live attempt's recovery point.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kdive.providers.external_boot_authority.protocol import MAX_MESSAGE_BYTES

MAX_RETAINED_KEYS = 2048

type ModuleVolumeKind = Literal["source.ext4", "scratch.ext4", "reaping.journal", "reaped.journal"]
type OperationNonce = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", strict=True)]


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModuleVolumeKeyV1(_ClosedValue):
    """One retained module volume, named by its owning attempt and kind."""

    system_id: UUID
    run_id: UUID
    operation_nonce: OperationNonce
    kind: ModuleVolumeKind


class ModuleVolumeSweepRequestV1(_ClosedValue):
    version: Literal["module-volume-sweep-v1"] = "module-volume-sweep-v1"
    retained: Annotated[tuple[ModuleVolumeKeyV1, ...], Field(max_length=MAX_RETAINED_KEYS)] = ()

    @model_validator(mode="after")
    def _keys_are_unique(self) -> ModuleVolumeSweepRequestV1:
        if len(set(self.retained)) != len(self.retained):
            raise ValueError("retained module volume keys must be unique")
        return self


class ModuleVolumeSweepResponseV1(_ClosedValue):
    version: Literal["module-volume-sweep-v1"] = "module-volume-sweep-v1"
    removed: Annotated[int, Field(strict=True, ge=0)]


def _canonical_bytes(value: BaseModel) -> bytes:
    payload = json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError
    return payload


def decode_module_volume_sweep_request(payload: bytes) -> ModuleVolumeSweepRequestV1:
    """Decode one canonical, bounded sweep request."""
    try:
        if type(payload) is not bytes or not payload or len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError
        value = ModuleVolumeSweepRequestV1.model_validate_json(payload)
        if _canonical_bytes(value) != payload:
            raise ValueError
        return value
    except ValidationError, ValueError, TypeError, UnicodeError:
        raise ValueError("invalid module volume sweep request") from None


def decode_module_volume_sweep_response(payload: bytes) -> ModuleVolumeSweepResponseV1:
    """Decode one canonical, bounded sweep response."""
    try:
        if type(payload) is not bytes or not payload or len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError
        value = ModuleVolumeSweepResponseV1.model_validate_json(payload)
        if _canonical_bytes(value) != payload:
            raise ValueError
        return value
    except ValidationError, ValueError, TypeError, UnicodeError:
        raise ValueError("invalid module volume sweep response") from None
