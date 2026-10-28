"""The worker reaps authority-owned module pools through each host's authority (#3386)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.module_volume_sweep import (
    MAX_RETAINED_KEYS,
    ModuleVolumeSweepRequestV1,
    ModuleVolumeSweepResponseV1,
)
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
from kdive.providers.remote_libvirt.reaping.module_volumes import RemoteLibvirtModuleVolumeReaper

_SYSTEM = "00000000-0000-4000-8000-000000000001"
_RUN = "00000000-0000-4000-8000-000000000002"


def _config(name: str, *, authority: bool = True) -> RemoteLibvirtConfig:
    binding = RemoteAuthorityBinding(name, "192.0.2.1", 9443, "ca", "cert", "key")
    return RemoteLibvirtConfig(
        uri=f"qemu+tls://{name}.example/system",
        cert_refs=TlsCertRefs("cert", "key", "ca"),
        concurrent_allocation_cap=1,
        authority=binding if authority else None,
        storage_pool=f"pool-{name}",
    )


def _key(index: int = 0, kind: str = "source.ext4") -> ModuleVolumeKey:
    return ModuleVolumeKey(_SYSTEM, _RUN, f"{index:032x}", kind)


class _Sender:
    """One host's authority; records every sweep request and answers or fails as told."""

    def __init__(self, removed: int = 0, failure: Exception | None = None) -> None:
        self.removed = removed
        self.failure = failure
        self.requests: list[ModuleVolumeSweepRequestV1] = []

    async def sweep_module_volumes(
        self, request: ModuleVolumeSweepRequestV1, *, deadline: float
    ) -> ModuleVolumeSweepResponseV1:
        del deadline
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ModuleVolumeSweepResponseV1(removed=self.removed)


def _reaper(
    configs: list[RemoteLibvirtConfig], senders: dict[str, _Sender] | None = None
) -> RemoteLibvirtModuleVolumeReaper:
    def factory(binding: RemoteAuthorityBinding) -> Any:
        return (senders or {})[binding.authority_instance]

    return RemoteLibvirtModuleVolumeReaper(lambda: configs, cast(Any, factory))


def _reap(reaper: RemoteLibvirtModuleVolumeReaper, keys: list[ModuleVolumeKey]) -> int:
    async def retained() -> list[ModuleVolumeKey]:
        return keys

    return asyncio.run(reaper.reap_module_volumes(retained))


def test_null_reaper_does_not_read_retention() -> None:
    called = False

    async def retained() -> list[ModuleVolumeKey]:
        nonlocal called
        called = True
        return []

    reaper: ModuleVolumeReaper = NullModuleVolumeReaper()
    assert asyncio.run(reaper.reap_module_volumes(retained)) == 0
    assert not called


def test_each_authority_host_sweeps_its_own_pool_with_the_whole_retained_set() -> None:
    first, second = _Sender(removed=2), _Sender(removed=3)
    keys = [_key(1), _key(2, "reaped.journal")]

    removed = _reap(
        _reaper([_config("first"), _config("second")], {"first": first, "second": second}), keys
    )

    assert removed == 5
    for sender in (first, second):
        assert [
            (item.system_id, item.run_id, item.operation_nonce, item.kind)
            for request in sender.requests
            for item in request.retained
        ] == [tuple(key) for key in keys]


def test_a_fleet_without_authority_hosts_does_no_io() -> None:
    async def retained() -> list[ModuleVolumeKey]:
        pytest.fail("a fleet with no authority host must not read retention")

    def factory(_binding: RemoteAuthorityBinding) -> Any:
        pytest.fail("a fleet with no authority host must not build a sender")

    reaper = RemoteLibvirtModuleVolumeReaper(
        lambda: [_config(str(i), authority=False) for i in range(3)], factory
    )

    assert asyncio.run(reaper.reap_module_volumes(retained)) == 0


def test_hosts_without_an_authority_are_skipped() -> None:
    sender = _Sender(removed=3)

    removed = _reap(
        _reaper([_config("plain", authority=False), _config("owned")], {"owned": sender}), []
    )

    assert removed == 3
    assert len(sender.requests) == 1


def test_a_configured_authority_host_without_a_sender_factory_fails() -> None:
    async def retained() -> list[ModuleVolumeKey]:
        pytest.fail("retention must not run without a sender")

    reaper = RemoteLibvirtModuleVolumeReaper(lambda: [_config("host")], None)

    with pytest.raises(CategorizedError) as caught:
        asyncio.run(reaper.reap_module_volumes(retained))

    assert caught.value.category is ErrorCategory.CONFLICT


@pytest.mark.parametrize(
    "failure",
    [
        CategorizedError("authority down", category=ErrorCategory.INFRASTRUCTURE_FAILURE),
        CategorizedError("refused", category=ErrorCategory.CONFLICT),
        OSError("connection refused"),
        TimeoutError(),
    ],
)
def test_an_unreachable_or_refusing_authority_fails_the_sweep(failure: Exception) -> None:
    with pytest.raises(CategorizedError) as caught:
        _reap(_reaper([_config("down")], {"down": _Sender(failure=failure)}), [_key()])

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_one_failing_host_does_not_stop_the_others_but_still_fails_the_sweep() -> None:
    healthy = _Sender(removed=4)

    with pytest.raises(CategorizedError) as caught:
        _reap(
            _reaper(
                [_config("down"), _config("up")],
                {"down": _Sender(failure=OSError("down")), "up": healthy},
            ),
            [],
        )

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert len(healthy.requests) == 1


def test_a_retained_set_over_the_bound_fails_without_truncating_or_sending() -> None:
    sender = _Sender(removed=9)
    keys = [_key(index) for index in range(MAX_RETAINED_KEYS + 1)]

    with pytest.raises(CategorizedError) as caught:
        _reap(_reaper([_config("host")], {"host": sender}), keys)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert sender.requests == []


def test_a_retained_set_at_the_bound_is_sent_whole() -> None:
    sender = _Sender()
    keys = [_key(index) for index in range(MAX_RETAINED_KEYS)]

    _reap(_reaper([_config("host")], {"host": sender}), keys)

    assert len(sender.requests[0].retained) == MAX_RETAINED_KEYS


def test_the_fleet_configuration_is_read_once_per_sweep() -> None:
    reads = 0
    sender = _Sender()

    def configs() -> list[RemoteLibvirtConfig]:
        nonlocal reads
        reads += 1
        return [_config("host")] if reads == 1 else []

    async def retained() -> list[ModuleVolumeKey]:
        return []

    reaper = RemoteLibvirtModuleVolumeReaper(configs, lambda _binding: cast(Any, sender))
    asyncio.run(reaper.reap_module_volumes(retained))

    assert reads == 1
    assert len(sender.requests) == 1
