"""Async fleet adapter for remote module-volume reaping."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.device_identity import DeviceIdentityInodeV1
from kdive.providers.infra.reaping import (
    ModuleVolumeKey,
    ModuleVolumeReaper,
    NullModuleVolumeReaper,
)
from kdive.providers.remote_libvirt.config import (
    RemoteAuthorityBinding,
    RemoteLibvirtConfig,
    TlsCertRefs,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.reaping.module_volumes import RemoteLibvirtModuleVolumeReaper


def _config(name: str, *, authority: bool = True) -> RemoteLibvirtConfig:
    binding = RemoteAuthorityBinding(name, "192.0.2.1", 9443, "ca", "cert", "key")
    return RemoteLibvirtConfig(
        uri=f"qemu+tls://{name}.example/system",
        cert_refs=TlsCertRefs("cert", "key", "ca"),
        concurrent_allocation_cap=1,
        authority=binding if authority else None,
        storage_pool=f"pool-{name}",
    )


class Fleet:
    def __init__(self, hosts: list[tuple[RemoteLibvirtConfig, object]]) -> None:
        self.hosts = hosts

    def configs(self) -> list[RemoteLibvirtConfig]:
        return [config for config, _ in self.hosts]

    @contextmanager
    def connection(self, config: RemoteLibvirtConfig) -> Iterator[object]:
        value = next(value for candidate, value in self.hosts if candidate is config)
        if isinstance(value, Exception):
            raise value
        yield value


class Sender:
    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths = paths

    async def resolve_device_identity(self, request: object, *, deadline: float) -> object:
        if self.paths is not None:
            self.paths.append(cast(Any, request).path)
        del deadline
        return DeviceIdentityInodeV1(primary=1, secondary=2)


def _reaper(
    fleet: Fleet,
    factory: Any = lambda _binding: Sender(),
) -> RemoteLibvirtModuleVolumeReaper:
    return RemoteLibvirtModuleVolumeReaper(
        cast(Any, fleet),
        RemoteModulePreparationExecutor(),
        factory,
    )


def test_null_reaper_does_not_read_retention() -> None:
    called = False

    async def retained() -> list[ModuleVolumeKey]:
        nonlocal called
        called = True
        return []

    reaper: ModuleVolumeReaper = NullModuleVolumeReaper()
    assert asyncio.run(reaper.reap_module_volumes(retained)) == 0
    assert not called


def test_fleet_runs_in_worker_and_bridges_retention_to_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    events: list[tuple[str, object]] = []
    bad, first, second = _config("bad"), _config("first"), _config("second")
    fleet = Fleet([(bad, RuntimeError("down")), (first, "conn-1"), (second, "conn-2")])
    bindings: list[RemoteAuthorityBinding] = []
    identity_paths: list[str] = []

    def sender_factory(binding: RemoteAuthorityBinding) -> Sender:
        bindings.append(binding)
        return Sender(identity_paths)

    def sync_reap(
        conn: object,
        pool: str,
        identity: object,
        *,
        retained_owners: object,
        deadline: float,
        clock: object,
    ) -> int:
        events.append(("worker", threading.get_ident()))
        events.append(("host", (conn, pool)))
        cast(Any, identity).identity("/candidate")
        cast(Any, identity).identity("/reference")
        owners = cast(Any, retained_owners)()
        events.append(("owners", owners))
        return 2

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.reaping.module_volumes.reap_orphaned_module_volumes",
        sync_reap,
    )

    async def scenario() -> int:
        async def retained() -> list[ModuleVolumeKey]:
            events.append(("callback", threading.get_ident()))
            return [ModuleVolumeKey("system", "run", "nonce", "source.ext4")]

        return await _reaper(fleet, sender_factory).reap_module_volumes(retained)

    assert asyncio.run(scenario()) == 4
    assert [event for event in events if event[0] == "host"] == [
        ("host", ("conn-1", "pool-first")),
        ("host", ("conn-2", "pool-second")),
    ]
    assert all(value != loop_thread for name, value in events if name == "worker")
    assert all(value == loop_thread for name, value in events if name == "callback")
    assert [binding.authority_instance for binding in bindings] == ["first", "second"]
    assert identity_paths == ["/candidate", "/reference", "/candidate", "/reference"]
    assert all(owners for name, owners in events if name == "owners")


def test_reachable_host_without_authority_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.reaping.module_volumes.reap_orphaned_module_volumes",
        lambda *_args, **_kwargs: pytest.fail("reap must not start without authority"),
    )

    async def scenario() -> None:
        nonlocal called

        async def retained() -> list[ModuleVolumeKey]:
            nonlocal called
            called = True
            return []

        with pytest.raises(CategorizedError) as caught:
            reaper = _reaper(Fleet([(_config("host", authority=False), object())]))
            await reaper.reap_module_volumes(retained)
        assert caught.value.category is ErrorCategory.CONFLICT

    asyncio.run(scenario())
    assert not called


def test_no_reachable_host_fails_for_worker_retry_without_reading_retention() -> None:
    called = False

    async def scenario() -> None:
        nonlocal called

        async def retained() -> list[ModuleVolumeKey]:
            nonlocal called
            called = True
            return []

        with pytest.raises(CategorizedError) as caught:
            await _reaper(Fleet([(_config("down"), RuntimeError("down"))])).reap_module_volumes(
                retained
            )
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(scenario())
    assert not called


def test_repeated_cancellation_waits_for_callback_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()
    callback_done = asyncio.Event()
    worker_release = threading.Event()

    def sync_reap(
        _conn: object,
        _pool: str,
        _identity: object,
        *,
        retained_owners: object,
        deadline: float,
        clock: object,
    ) -> int:
        cast(Any, retained_owners)()
        worker_release.wait()
        return 1

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.reaping.module_volumes.reap_orphaned_module_volumes",
        sync_reap,
    )

    async def scenario() -> None:
        async def retained() -> list[ModuleVolumeKey]:
            callback_started.set()
            await callback_release.wait()
            callback_done.set()
            return []

        task = asyncio.create_task(
            _reaper(Fleet([(_config("host"), object())])).reap_module_volumes(retained)
        )
        await callback_started.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert task.cancelling() == 2
        callback_release.set()
        await callback_done.wait()
        await asyncio.sleep(0)
        assert not task.done()
        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelling() == 2

    asyncio.run(scenario())
