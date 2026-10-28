"""The worker reaps authority-owned module pools through each host's authority (#3386)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.module_volume_sweep import (
    MAX_RETAINED_KEYS,
    MAX_SWEEP_CANDIDATES,
    ModuleVolumeKeyV1,
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


_KEY0 = _key(0)


class _Sender:
    """One host's authority: lists the volumes it owns, then sweeps what the worker names."""

    def __init__(
        self,
        removed: int = 0,
        failure: Exception | None = None,
        owned: tuple[ModuleVolumeKey, ...] = (_KEY0,),
        events: list[str] | None = None,
    ) -> None:
        self.removed = removed
        self.failure = failure
        self.owned = owned
        self.events = events if events is not None else []
        self.requests: list[ModuleVolumeSweepRequestV1] = []

    async def sweep_module_volumes(
        self, request: ModuleVolumeSweepRequestV1, *, deadline: float
    ) -> ModuleVolumeSweepResponseV1:
        del deadline
        self.requests.append(request)
        self.events.append(f"{request.phase}")
        if self.failure is not None:
            raise self.failure
        if request.phase == "list":
            return ModuleVolumeSweepResponseV1(
                removed=0,
                owned=tuple(
                    ModuleVolumeKeyV1(
                        system_id=UUID(key.system_id),
                        run_id=UUID(key.run_id),
                        operation_nonce=key.operation_nonce,
                        kind=cast(Any, key.kind),
                    )
                    for key in self.owned
                ),
            )
        return ModuleVolumeSweepResponseV1(removed=self.removed)

    @property
    def sweeps(self) -> list[ModuleVolumeSweepRequestV1]:
        return [request for request in self.requests if request.phase == "sweep"]


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
            (str(item.system_id), str(item.run_id), item.operation_nonce, item.kind)
            for request in sender.sweeps
            for item in request.retained
        ] == [tuple(key) for key in keys]


def test_retention_is_read_after_every_host_is_listed_and_candidates_are_the_listing() -> None:
    events: list[str] = []
    first = _Sender(owned=(_key(7),), events=events)
    second = _Sender(owned=(_key(8), _key(9)), events=events)

    async def retained() -> list[ModuleVolumeKey]:
        events.append("retention")
        return []

    reaper = _reaper([_config("first"), _config("second")], {"first": first, "second": second})
    asyncio.run(reaper.reap_module_volumes(retained))

    assert events == ["list", "list", "retention", "sweep", "sweep"]
    assert [item.operation_nonce for item in first.sweeps[0].candidates] == [f"{7:032x}"]
    assert [item.operation_nonce for item in second.sweeps[0].candidates] == [
        f"{8:032x}",
        f"{9:032x}",
    ]


def test_a_host_with_no_owned_volumes_is_not_swept_and_retention_is_not_read() -> None:
    sender = _Sender(owned=())

    async def retained() -> list[ModuleVolumeKey]:
        pytest.fail("nothing is listed, so there is nothing to protect")

    reaper = _reaper([_config("host")], {"host": sender})

    assert asyncio.run(reaper.reap_module_volumes(retained)) == 0
    assert sender.sweeps == []


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
    assert len(sender.sweeps) == 1


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
    assert len(healthy.sweeps) == 1


def test_a_retained_set_over_the_bound_fails_without_truncating_or_sending() -> None:
    sender = _Sender(removed=9)
    keys = [_key(index) for index in range(MAX_RETAINED_KEYS + 1)]

    with pytest.raises(CategorizedError) as caught:
        _reap(_reaper([_config("host")], {"host": sender}), keys)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert sender.sweeps == []


def test_a_retained_set_at_the_bound_is_sent_whole() -> None:
    sender = _Sender()
    keys = [_key(index) for index in range(MAX_RETAINED_KEYS)]

    _reap(_reaper([_config("host")], {"host": sender}), keys)

    assert len(sender.sweeps[0].retained) == MAX_RETAINED_KEYS


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
    assert [request.phase for request in sender.requests] == ["list", "sweep"]


def test_a_list_failure_on_one_host_still_sweeps_the_others() -> None:
    healthy = _Sender(removed=4)

    with pytest.raises(CategorizedError):
        _reap(
            _reaper(
                [_config("down"), _config("up")],
                {"down": _Sender(failure=TimeoutError()), "up": healthy},
            ),
            [],
        )

    assert len(healthy.sweeps) == 1


def test_a_sender_that_cannot_be_built_fails_that_host_only() -> None:
    healthy = _Sender(removed=1)

    def factory(binding: RemoteAuthorityBinding) -> Any:
        if binding.authority_instance == "broken":
            raise CategorizedError("no credential", category=ErrorCategory.CONFIGURATION_ERROR)
        return healthy

    reaper = RemoteLibvirtModuleVolumeReaper(
        lambda: [_config("broken"), _config("fine")], cast(Any, factory)
    )

    async def retained() -> list[ModuleVolumeKey]:
        return []

    with pytest.raises(CategorizedError):
        asyncio.run(reaper.reap_module_volumes(retained))

    assert len(healthy.sweeps) == 1


def test_a_large_listing_is_swept_in_slices_with_the_whole_retained_set() -> None:
    owned = tuple(_key(index) for index in range(MAX_SWEEP_CANDIDATES * 2 + 452))
    sender = _Sender(removed=1, owned=owned)
    retained = [_key(10_000), _key(10_001)]

    removed = _reap(_reaper([_config("host")], {"host": sender}), retained)

    assert removed == 3
    assert [len(request.candidates) for request in sender.sweeps] == [
        MAX_SWEEP_CANDIDATES,
        MAX_SWEEP_CANDIDATES,
        452,
    ]
    assert all(len(request.retained) == len(retained) for request in sender.sweeps)
    assert [item.operation_nonce for request in sender.sweeps for item in request.candidates] == [
        key.operation_nonce for key in owned
    ]
