"""Closed remote device-identity wire values (ADR-0604)."""

from __future__ import annotations

import asyncio
import json
import posixpath
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.protocol import MAX_MESSAGE_BYTES

if TYPE_CHECKING:
    from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
        RemoteDeviceIdentity,
    )

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


class _IdentityLookup(Protocol):
    def __call__(self, path: str) -> RemoteDeviceIdentity | None: ...


class _IdentitySender(Protocol):
    async def resolve_device_identity(
        self, request: DeviceIdentityRequestV1, *, deadline: float
    ) -> DeviceIdentityResponseV1: ...


class RemoteAuthorityDeviceIdentity:
    """Synchronous ADR-0603 port over one fixed Resource-bound sender."""

    def __init__(
        self,
        sender: _IdentitySender,
        preparation_deadline: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sender = sender
        self._preparation_deadline = preparation_deadline
        self._clock = clock

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
            RemoteDeviceIdentity,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CategorizedError(
                "remote device identity lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        remaining = self._preparation_deadline - self._clock()
        if remaining <= 0:
            raise CategorizedError(
                "remote device identity lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        request = DeviceIdentityRequestV1(path=path)

        async def resolve() -> DeviceIdentityResponseV1:
            loop = asyncio.get_running_loop()
            return await self._sender.resolve_device_identity(
                request, deadline=loop.time() + remaining
            )

        try:
            response = asyncio.run(resolve())
        except CategorizedError:
            raise
        except Exception:
            raise CategorizedError(
                "remote device identity lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from None
        if isinstance(response, DeviceIdentityAbsentV1):
            return None
        if isinstance(response, DeviceIdentityBlockV1):
            return RemoteDeviceIdentity(kind="block", primary=response.primary, secondary=0)
        if isinstance(response, DeviceIdentityInodeV1):
            return RemoteDeviceIdentity(
                kind="inode", primary=response.primary, secondary=response.secondary
            )
        raise CategorizedError("remote device identity is invalid", category=ErrorCategory.CONFLICT)


def build_remote_device_identity_port(
    sender: _IdentitySender | None, preparation_deadline: float
) -> RemoteAuthorityDeviceIdentity | None:
    """Build the port only for a configured Resource-bound authority sender."""
    if sender is None:
        return None
    return RemoteAuthorityDeviceIdentity(sender, preparation_deadline)


class RemoteDeviceIdentityService:
    """Bounded provider-host filesystem identity lookup service."""

    def __init__(
        self,
        identity: _IdentityLookup | None = None,
        *,
        capacity: int = 4,
    ) -> None:
        if capacity < 1 or capacity > 4:
            raise ValueError("identity lookup capacity must be between one and four")
        if identity is None:
            from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
                HostStatDeviceIdentity,
            )

            identity = HostStatDeviceIdentity().identity
        self._identity = identity
        self._executor = ThreadPoolExecutor(
            max_workers=capacity, thread_name_prefix="kdive-device-identity"
        )
        self._admission = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._closed = False

    async def resolve(self, request: DeviceIdentityRequestV1) -> DeviceIdentityResponseV1:
        with self._lock:
            if self._closed or not self._admission.acquire(blocking=False):
                raise RuntimeError("provider-failure")
            try:
                future = self._executor.submit(self._identity, request.path)
            except BaseException:
                self._admission.release()
                raise RuntimeError("provider-failure") from None
        future.add_done_callback(lambda _future: self._admission.release())
        try:
            result = await asyncio.shield(asyncio.wrap_future(future))
        except BaseException:
            if future.cancelled():
                raise RuntimeError("provider-failure") from None
            raise
        if result is None:
            return DeviceIdentityAbsentV1()
        from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
            RemoteDeviceIdentity,
        )

        if type(result) is not RemoteDeviceIdentity:
            raise RuntimeError("provider-failure")
        if result.kind == "block":
            return DeviceIdentityBlockV1(primary=result.primary)
        return DeviceIdentityInodeV1(primary=result.primary, secondary=result.secondary)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)
