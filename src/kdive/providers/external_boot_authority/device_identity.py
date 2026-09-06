"""Closed remote device-identity wire values (ADR-0604)."""

from __future__ import annotations

import json
import posixpath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from kdive.providers.external_boot_authority.protocol import MAX_MESSAGE_BYTES

_MAX_PATH_BYTES = 4_096
_MAX_IDENTITY_COMPONENT = 2**64 - 1


def _canonical_bytes(value: BaseModel) -> bytes:
    payload = json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError
    return payload


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DeviceIdentityRequestV1(_ClosedValue):
    version: Literal["device-identity-v1"] = "device-identity-v1"
    path: str

    @field_validator("path")
    @classmethod
    def _path_is_canonical(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or "\0" in value
            or len(value.encode("utf-8")) > _MAX_PATH_BYTES
            or posixpath.normpath(value) != value
        ):
            raise ValueError("path must be a normalized absolute path")
        return value


class DeviceIdentityAbsentV1(_ClosedValue):
    version: Literal["device-identity-v1"] = "device-identity-v1"
    kind: Literal["absent"] = "absent"


type IdentityComponent = Annotated[int, Field(strict=True, ge=0, le=_MAX_IDENTITY_COMPONENT)]


class DeviceIdentityInodeV1(_ClosedValue):
    version: Literal["device-identity-v1"] = "device-identity-v1"
    kind: Literal["inode"] = "inode"
    primary: IdentityComponent
    secondary: IdentityComponent


class DeviceIdentityBlockV1(_ClosedValue):
    version: Literal["device-identity-v1"] = "device-identity-v1"
    kind: Literal["block"] = "block"
    primary: IdentityComponent
    secondary: Literal[0] = 0

    @field_validator("secondary", mode="before")
    @classmethod
    def _secondary_is_integer_zero(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("block secondary must be integer zero")
        return value


type DeviceIdentityResponseV1 = Annotated[
    DeviceIdentityAbsentV1 | DeviceIdentityInodeV1 | DeviceIdentityBlockV1,
    Field(discriminator="kind"),
]
_RESPONSE_ADAPTER = TypeAdapter(DeviceIdentityResponseV1)


def decode_device_identity_request(payload: bytes) -> DeviceIdentityRequestV1:
    """Decode one canonical, bounded device-identity request."""
    try:
        if type(payload) is not bytes or not payload or len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError
        value = DeviceIdentityRequestV1.model_validate_json(payload)
        if _canonical_bytes(value) != payload:
            raise ValueError
        return value
    except ValidationError, ValueError, TypeError, UnicodeError:
        raise ValueError("invalid device identity request") from None


def decode_device_identity_response(payload: bytes) -> DeviceIdentityResponseV1:
    """Decode one canonical, bounded device-identity response."""
    try:
        if type(payload) is not bytes or not payload or len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError
        value = _RESPONSE_ADAPTER.validate_json(payload)
        if _canonical_bytes(value) != payload:
            raise ValueError
        return value
    except ValidationError, ValueError, TypeError, UnicodeError:
        raise ValueError("invalid device identity response") from None
