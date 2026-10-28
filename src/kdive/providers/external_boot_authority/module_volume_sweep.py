"""Closed wire values for the authority-side module-volume sweep (ADR-0588, ADR-0603).

The exchange has two phases so the retained set is always read after the volumes are listed,
which is what makes a listed volume older than the retained snapshot: ``list`` returns the
authority's owned module volumes; the worker then reads the retained attempts and sends ``sweep``
with the volumes it saw (the candidates, in slices) and the retained set. The authority deletes
only a candidate that is not retained and not referenced by a domain, so a volume created after
the listing is never a candidate. Every set is bounded and never truncated: a set over the bound is
refused rather than reduced, because deleting around a truncated set would destroy a live
attempt's recovery point.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kdive.providers.external_boot_authority.protocol import MAX_MESSAGE_BYTES

MAX_RETAINED_KEYS = 4096
MAX_OWNED_KEYS = MAX_RETAINED_KEYS
# One sweep request carries the whole retained set, so its candidates are sent in slices that keep
# a full request under the wire limit (4096 + 1024 keys of at most 178 bytes each).
MAX_SWEEP_CANDIDATES = 1024

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
    phase: Literal["list", "sweep"] = "sweep"
    retained: Annotated[tuple[ModuleVolumeKeyV1, ...], Field(max_length=MAX_RETAINED_KEYS)] = ()
    candidates: Annotated[
        tuple[ModuleVolumeKeyV1, ...], Field(max_length=MAX_SWEEP_CANDIDATES)
    ] = ()

    @model_validator(mode="after")
    def _shape_matches_the_phase(self) -> ModuleVolumeSweepRequestV1:
        if self.phase == "list" and (self.retained or self.candidates):
            raise ValueError("a list request carries no retained or candidate keys")
        for keys in (self.retained, self.candidates):
            if len(set(keys)) != len(keys):
                raise ValueError("module volume keys must be unique")
        return self


class ModuleVolumeSweepResponseV1(_ClosedValue):
    version: Literal["module-volume-sweep-v1"] = "module-volume-sweep-v1"
    removed: Annotated[int, Field(strict=True, ge=0)]
    owned: Annotated[tuple[ModuleVolumeKeyV1, ...], Field(max_length=MAX_OWNED_KEYS)] = ()


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
