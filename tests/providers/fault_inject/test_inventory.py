"""The mock infra-inventory seam: provision records a domain the reaper can find/reap."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")
_OTHER = UUID("22222222-2222-2222-2222-222222222222")


def test_recorded_domain_is_listed_by_the_reaper() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    owned = asyncio.run(FaultInjectReaper(inventory).list_owned())

    assert [(d.name, d.system_id) for d in owned] == [("fault-inject-domain-1", _SYSTEM)]


def test_destroy_removes_the_domain_from_the_inventory() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    reaper = FaultInjectReaper(inventory)

    asyncio.run(reaper.destroy("fault-inject-domain-1"))

    assert asyncio.run(reaper.list_owned()) == []


def test_destroy_is_idempotent_for_an_unknown_domain() -> None:
    reaper = FaultInjectReaper(FaultInjectInventory())

    # A reaper destroy that races a concurrent teardown must not raise (loop.py contract).
    asyncio.run(reaper.destroy("never-provisioned"))

    assert asyncio.run(reaper.list_owned()) == []


def test_forget_drops_a_domain_so_a_torn_down_system_is_not_reaped_twice() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    inventory.forget("fault-inject-domain-1")

    assert inventory.owned_domains() == []


def test_flag_orphan_marks_only_the_named_domain_and_keeps_it_owned() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    inventory.record(_OTHER, "fault-inject-domain-2")

    inventory.flag_orphan("fault-inject-domain-1")

    # The flagged domain is still owned (orphan-flagged leaves the entry for the reaper).
    assert {d.name for d in inventory.owned_domains()} == {
        "fault-inject-domain-1",
        "fault-inject-domain-2",
    }
    assert inventory.is_orphaned("fault-inject-domain-1") is True
    # The flag is per-domain: a sibling domain is not collaterally flagged.
    assert inventory.is_orphaned("fault-inject-domain-2") is False


def test_forget_clears_the_orphan_flag_so_a_reused_name_is_not_stale() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    inventory.flag_orphan("fault-inject-domain-1")

    inventory.forget("fault-inject-domain-1")
    # A later System reusing the same domain name must not inherit a stale orphan flag.
    inventory.record(_OTHER, "fault-inject-domain-1")

    assert inventory.is_orphaned("fault-inject-domain-1") is False


def test_flag_orphan_is_idempotent() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    inventory.flag_orphan("fault-inject-domain-1")
    inventory.flag_orphan("fault-inject-domain-1")

    assert inventory.is_orphaned("fault-inject-domain-1") is True


def test_is_orphaned_is_false_for_an_unknown_domain() -> None:
    inventory = FaultInjectInventory()

    # An unrecorded name is simply not orphaned — no KeyError.
    assert inventory.is_orphaned("never-provisioned") is False


def test_reaper_satisfies_the_reconciler_infra_reaper_protocol() -> None:
    from kdive.providers.infra.reaping import InfraReaper, OwnedDomain

    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    reaper = FaultInjectReaper(inventory)

    # The spec names "the InfraReaper shape" — assert the mock seam the reconciler
    # leaked-domain pass consumes is structurally that port, not a lookalike.
    assert isinstance(reaper, InfraReaper)
    (owned,) = asyncio.run(reaper.list_owned())
    assert isinstance(owned, OwnedDomain)


def test_next_draw_counts_per_domain_from_one_and_restarts_on_record_and_forget() -> None:
    inventory = FaultInjectInventory()
    system = uuid4()
    inventory.record(system, "dom-a")
    inventory.record(uuid4(), "dom-b")

    assert [inventory.next_draw("dom-a") for _ in range(2)] == [(system, 1), (system, 2)]
    assert inventory.next_draw("dom-b") is not None
    inventory.record(system, "dom-a")
    assert inventory.next_draw("dom-a") == (system, 1)
    inventory.forget("dom-a")
    assert inventory.next_draw("dom-a") is None


def test_next_draw_hands_each_concurrent_caller_a_distinct_ordinal() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "dom-a")
    with ThreadPoolExecutor(max_workers=8) as pool:
        draws = list(pool.map(lambda _: inventory.next_draw("dom-a"), range(200)))

    assert sorted(draw[1] for draw in draws if draw) == list(range(1, 201))


class _MutatingDict(dict[str, UUID]):
    """A domain map that lets another thread mutate it while ``items()`` is half-consumed."""

    def __init__(self, mutate: Callable[[], None]) -> None:
        super().__init__()
        self._mutate = mutate

    def items(self) -> Iterator[tuple[str, UUID]]:  # ty: ignore[invalid-method-override]
        entries = iter(super().items())
        yield next(entries)
        self._mutate()
        yield from entries


def test_owned_domains_snapshot_survives_a_record_landing_mid_listing() -> None:
    inventory = FaultInjectInventory()
    writer = threading.Thread(target=inventory.record, args=(_OTHER, "dom-new"))

    def land_record_mid_iteration() -> None:
        writer.start()
        # Unlocked, the writer finishes here and the iteration raises RuntimeError; locked, the
        # writer blocks until the listing returns, so this wait times out by design.
        writer.join(timeout=0.2)

    inventory._domains = _MutatingDict(land_record_mid_iteration)
    inventory._domains["dom-a"] = _SYSTEM
    inventory._domains["dom-b"] = _SYSTEM

    listed = inventory.owned_domains()
    writer.join()

    assert {d.name for d in listed} == {"dom-a", "dom-b"}
    assert inventory.system_id_of("dom-new") == _OTHER


def test_flag_orphan_after_forget_stores_nothing() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "dom-a")
    inventory.forget("dom-a")

    inventory.flag_orphan("dom-a")
    inventory.record(_OTHER, "dom-a")

    assert inventory.is_orphaned("dom-a") is False


def test_readers_wait_for_the_inventory_lock() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "dom-a")
    readers = {
        "owned_domains": inventory.owned_domains,
        "is_orphaned": lambda: inventory.is_orphaned("dom-a"),
        "system_id_of": lambda: inventory.system_id_of("dom-a"),
        "flag_orphan": lambda: inventory.flag_orphan("dom-a"),
    }
    for name, call in readers.items():
        thread = threading.Thread(target=call, name=name)
        with inventory._lock:
            thread.start()
            thread.join(timeout=0.2)
            assert thread.is_alive(), f"{name} did not take the inventory lock"
        thread.join()
