"""Closed wire values for the authority-side module-volume sweep (ADR-0588, ADR-0603)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from kdive.providers.external_boot_authority.module_volume_sweep import (
    MAX_RETAINED_KEYS,
    ModuleVolumeKeyV1,
    ModuleVolumeKind,
    ModuleVolumeSweepRequestV1,
    ModuleVolumeSweepResponseV1,
    decode_module_volume_sweep_request,
    decode_module_volume_sweep_response,
)

_SYSTEM = "00000000-0000-4000-8000-000000000001"
_RUN = "00000000-0000-4000-8000-000000000002"
_NONCE = "3" * 32


def _wire(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _key(kind: str = "source.ext4", nonce: str = _NONCE) -> dict[str, str]:
    return {"kind": kind, "operation_nonce": nonce, "run_id": _RUN, "system_id": _SYSTEM}


def _key_value(kind: ModuleVolumeKind) -> ModuleVolumeKeyV1:
    return ModuleVolumeKeyV1(
        system_id=UUID(_SYSTEM),
        run_id=UUID(_RUN),
        operation_nonce=_NONCE,
        kind=kind,
    )


def _request(keys: list[dict[str, str]]) -> bytes:
    return _wire({"retained": keys, "version": "module-volume-sweep-v1"})


def test_request_has_one_canonical_shape() -> None:
    decoded = decode_module_volume_sweep_request(_request([_key(), _key("reaped.journal")]))

    assert decoded == ModuleVolumeSweepRequestV1(
        retained=(
            ModuleVolumeKeyV1(
                system_id=UUID(_SYSTEM),
                run_id=UUID(_RUN),
                operation_nonce=_NONCE,
                kind="source.ext4",
            ),
            ModuleVolumeKeyV1(
                system_id=UUID(_SYSTEM),
                run_id=UUID(_RUN),
                operation_nonce=_NONCE,
                kind="reaped.journal",
            ),
        )
    )


def test_an_empty_retained_set_is_a_valid_request() -> None:
    assert decode_module_volume_sweep_request(_request([])) == ModuleVolumeSweepRequestV1()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        _wire({"retained": [], "version": "module-volume-sweep-v2"}),
        _wire({"retained": [], "version": "module-volume-sweep-v1", "extra": 1}),
        _wire({"version": "module-volume-sweep-v1"}),
        json.dumps({"retained": [], "version": "module-volume-sweep-v1"}).encode(),
        _request([{**_key(), "kind": "other.ext4"}]),
        _request([{**_key(), "operation_nonce": "G" * 32}]),
        _request([{**_key(), "operation_nonce": "3" * 31}]),
        _request([{**_key(), "system_id": "not-a-uuid"}]),
        _request([{**_key(), "extra": "x"}]),
        _request([_key(), _key()]),
        b"x" * 2_000_000,
    ],
)
def test_request_rejects_anything_but_the_canonical_closed_shape(payload: bytes) -> None:
    with pytest.raises(ValueError, match="invalid module volume sweep request"):
        decode_module_volume_sweep_request(payload)


def test_request_bound_is_exact() -> None:
    def keys(count: int) -> list[dict[str, str]]:
        return [_key(nonce=f"{index:032x}") for index in range(count)]

    decoded = decode_module_volume_sweep_request(_request(keys(MAX_RETAINED_KEYS)))
    assert len(decoded.retained) == MAX_RETAINED_KEYS

    with pytest.raises(ValueError, match="invalid module volume sweep request"):
        decode_module_volume_sweep_request(_request(keys(MAX_RETAINED_KEYS + 1)))


def test_response_carries_one_non_negative_count() -> None:
    payload = _wire({"removed": 3, "version": "module-volume-sweep-v1"})
    assert decode_module_volume_sweep_response(payload) == ModuleVolumeSweepResponseV1(removed=3)


@pytest.mark.parametrize(
    "value",
    [
        {"removed": -1, "version": "module-volume-sweep-v1"},
        {"removed": True, "version": "module-volume-sweep-v1"},
        {"removed": "3", "version": "module-volume-sweep-v1"},
        {"removed": 1, "version": "module-volume-sweep-v1", "extra": 1},
        {"version": "module-volume-sweep-v1"},
        {"removed": 1, "version": "other"},
    ],
)
def test_response_rejects_anything_but_the_closed_shape(value: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid module volume sweep response"):
        decode_module_volume_sweep_response(_wire(value))
