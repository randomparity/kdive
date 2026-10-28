"""Closed wire values for the authority-side module-volume sweep (ADR-0588, ADR-0603)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

MAX_RETAINED_KEYS = 2048


class ModuleVolumeKeyV1(BaseModel):
    system_id: str
    run_id: str
    operation_nonce: str
    kind: str


class ModuleVolumeSweepRequestV1(BaseModel):
    version: Literal["module-volume-sweep-v1"] = "module-volume-sweep-v1"
    retained: tuple[ModuleVolumeKeyV1, ...] = ()


class ModuleVolumeSweepResponseV1(BaseModel):
    version: Literal["module-volume-sweep-v1"] = "module-volume-sweep-v1"
    removed: int = 0


def decode_module_volume_sweep_request(payload: bytes) -> ModuleVolumeSweepRequestV1:
    raise NotImplementedError


def decode_module_volume_sweep_response(payload: bytes) -> ModuleVolumeSweepResponseV1:
    raise NotImplementedError
