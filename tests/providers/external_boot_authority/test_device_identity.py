"""Closed remote device-identity wire values (ADR-0604)."""

import json

import pytest

from kdive.providers.external_boot_authority.device_identity import (
    DeviceIdentityAbsentV1,
    DeviceIdentityBlockV1,
    DeviceIdentityInodeV1,
    DeviceIdentityRequestV1,
    decode_device_identity_request,
    decode_device_identity_response,
)


def _wire(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_device_identity_request_has_one_canonical_bounded_shape() -> None:
    payload = _wire({"path": "/var/lib/images/disk", "version": "device-identity-v1"})
    assert decode_device_identity_request(payload) == DeviceIdentityRequestV1(
        path="/var/lib/images/disk"
    )


@pytest.mark.parametrize(
    "path",
    ["relative", "/not/../normalized", "/contains\0nul", "/" + "x" * 4096],
)
def test_device_identity_request_rejects_invalid_paths(path: str) -> None:
    with pytest.raises(ValueError, match="invalid device identity request"):
        decode_device_identity_request(_wire({"path": path, "version": "device-identity-v1"}))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"kind": "absent", "version": "device-identity-v1"},
            DeviceIdentityAbsentV1(),
        ),
        (
            {"kind": "inode", "primary": 8, "secondary": 11, "version": "device-identity-v1"},
            DeviceIdentityInodeV1(primary=8, secondary=11),
        ),
        (
            {"kind": "block", "primary": 22, "secondary": 0, "version": "device-identity-v1"},
            DeviceIdentityBlockV1(primary=22),
        ),
    ],
)
def test_device_identity_response_round_trips(value: object, expected: object) -> None:
    assert decode_device_identity_response(_wire(value)) == expected


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "unknown", "version": "device-identity-v1"},
        {"kind": "absent", "primary": 1, "version": "device-identity-v1"},
        {"kind": "inode", "primary": True, "secondary": 1, "version": "device-identity-v1"},
        {"kind": "inode", "primary": -1, "secondary": 1, "version": "device-identity-v1"},
        {"kind": "block", "primary": 1, "secondary": 1, "version": "device-identity-v1"},
        {"kind": "block", "primary": 2**64, "secondary": 0, "version": "device-identity-v1"},
    ],
)
def test_device_identity_response_rejects_other_shapes(value: object) -> None:
    with pytest.raises(ValueError, match="invalid device identity response"):
        decode_device_identity_response(_wire(value))


def test_device_identity_decoders_require_canonical_bounded_bytes() -> None:
    with pytest.raises(ValueError, match="invalid device identity request"):
        decode_device_identity_request(b'{"version": "device-identity-v1", "path": "/x"}')
    with pytest.raises(ValueError, match="invalid device identity response"):
        decode_device_identity_response(b"{" + b" " * 1_048_576 + b"}")
