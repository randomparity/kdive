"""Closed remote device-identity wire values (ADR-0604)."""

import asyncio
import json
import threading
from concurrent.futures import Future

import pytest

from kdive.providers.external_boot_authority.device_identity import (
    DeviceIdentityAbsentV1,
    DeviceIdentityBlockV1,
    DeviceIdentityInodeV1,
    DeviceIdentityRequestV1,
    RemoteAuthorityDeviceIdentity,
    RemoteDeviceIdentityService,
    build_remote_device_identity_port,
    decode_device_identity_request,
    decode_device_identity_response,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
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


def test_identity_service_rejects_exact_capacity_and_recovers_on_completion() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked(path: str) -> RemoteDeviceIdentity | None:
        del path
        started.set()
        release.wait()
        return None

    async def exercise() -> None:
        service = RemoteDeviceIdentityService(identity=blocked, capacity=1)
        first = asyncio.create_task(service.resolve(DeviceIdentityRequestV1(path="/one")))
        await asyncio.to_thread(started.wait, 1)
        with pytest.raises(RuntimeError, match="provider-failure"):
            await service.resolve(DeviceIdentityRequestV1(path="/two"))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(RuntimeError, match="provider-failure"):
            await service.resolve(DeviceIdentityRequestV1(path="/three"))
        release.set()
        await asyncio.sleep(0.05)
        assert await service.resolve(DeviceIdentityRequestV1(path="/four")) == (
            DeviceIdentityAbsentV1()
        )
        service.close()

    asyncio.run(exercise())


def test_repeated_cancellation_does_not_consume_default_executor_or_release_slots() -> None:
    started = threading.Barrier(3)
    release = threading.Event()

    def blocked(path: str) -> RemoteDeviceIdentity | None:
        del path
        started.wait()
        release.wait()
        return None

    async def exercise() -> None:
        service = RemoteDeviceIdentityService(identity=blocked, capacity=2)
        tasks = [
            asyncio.create_task(service.resolve(DeviceIdentityRequestV1(path=f"/{index}")))
            for index in range(2)
        ]
        await asyncio.to_thread(started.wait)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "ready"), 0.5) == "ready"
        with pytest.raises(RuntimeError, match="provider-failure"):
            await service.resolve(DeviceIdentityRequestV1(path="/exhausted"))
        release.set()
        await asyncio.sleep(0.05)
        service.close()

    asyncio.run(exercise())


def test_identity_service_close_is_nonwaiting_and_rejects_new_work() -> None:
    pending: Future[RemoteDeviceIdentity | None] = Future()

    def wait_for_result(path: str) -> RemoteDeviceIdentity | None:
        del path
        return pending.result()

    service = RemoteDeviceIdentityService(identity=wait_for_result, capacity=1)

    async def begin() -> asyncio.Task[object]:
        task = asyncio.create_task(service.resolve(DeviceIdentityRequestV1(path="/one")))
        await asyncio.sleep(0.01)
        return task

    task = asyncio.run(begin())
    service.close()
    pending.set_result(None)
    task.cancel()
    with pytest.raises(RuntimeError, match="provider-failure"):
        asyncio.run(service.resolve(DeviceIdentityRequestV1(path="/two")))


def test_synchronous_identity_adapter_uses_captured_deadline_and_maps_identity() -> None:
    seen: list[tuple[str, float]] = []

    class Sender:
        async def resolve_device_identity(
            self, request: DeviceIdentityRequestV1, *, deadline: float
        ) -> DeviceIdentityInodeV1:
            seen.append((request.path, deadline - asyncio.get_running_loop().time()))
            return DeviceIdentityInodeV1(primary=4, secondary=5)

    port = RemoteAuthorityDeviceIdentity(Sender(), 110.0, clock=lambda: 100.0)
    assert port.identity("/disk") == RemoteDeviceIdentity("inode", 4, 5)
    assert seen[0][0] == "/disk"
    assert seen[0][1] == pytest.approx(10.0)


def test_synchronous_identity_adapter_rejects_expiry_and_running_loop() -> None:
    class Sender:
        called = False

        async def resolve_device_identity(
            self, request: DeviceIdentityRequestV1, *, deadline: float
        ) -> DeviceIdentityAbsentV1:
            del request, deadline
            self.called = True
            return DeviceIdentityAbsentV1()

    sender = Sender()
    with pytest.raises(Exception, match="remote device identity lookup failed"):
        RemoteAuthorityDeviceIdentity(sender, 100.0, clock=lambda: 100.0).identity("/disk")

    async def exercise() -> None:
        with pytest.raises(Exception, match="remote device identity lookup failed"):
            RemoteAuthorityDeviceIdentity(sender, 110.0, clock=lambda: 100.0).identity("/disk")

    asyncio.run(exercise())
    assert sender.called is False


def test_unconfigured_device_identity_port_is_absent() -> None:
    assert build_remote_device_identity_port(None, 100.0) is None
