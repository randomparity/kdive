"""The provider-agnostic capture-reclamation port and its disabled wiring (ADR-0556)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from kdive.providers.infra.reaping import (
    CaptureReaper,
    NullCaptureReaper,
    OrphanedCapture,
    dispatchable_capture_kinds,
)
from kdive.providers.ports.traffic import capture_qom_id


class _ConcreteReaper:
    """A reaper that records what it was handed and reports what it reclaimed."""

    def __init__(self, *, reclaimed: bool = True) -> None:
        self.reclaimed = reclaimed
        self.seen: list[OrphanedCapture] = []

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        self.seen.append(capture)
        return self.reclaimed


def _capture(kind: str = "remote-libvirt") -> OrphanedCapture:
    return OrphanedCapture(
        provider_kind=kind,
        resource_id=uuid4(),
        resource_name="host-a",
        system_id=uuid4(),
        domain_name="kdive-guest",
        job_id=uuid4(),
    )


def test_an_orphaned_capture_names_only_its_own_job_sink() -> None:
    """The port carries what a reaper needs to derive one sink name and nothing wider."""
    capture = _capture()

    assert capture_qom_id(capture.job_id).endswith(str(capture.job_id))
    assert capture.domain_name == "kdive-guest"
    assert capture.resource_name == "host-a"


def test_a_concrete_reaper_satisfies_the_port() -> None:
    reaper = _ConcreteReaper()

    assert isinstance(reaper, CaptureReaper)
    assert isinstance(NullCaptureReaper(), CaptureReaper)


def test_the_null_reaper_reclaims_nothing() -> None:
    """Disabled wiring can never report a reclaim, so it can never mark a row complete."""

    async def _run() -> None:
        assert await NullCaptureReaper().reclaim_capture(_capture()) is False

    asyncio.run(_run())


def test_a_concrete_reaper_reports_what_it_did() -> None:
    capture = _capture()
    reclaimed = _ConcreteReaper(reclaimed=True)
    declined = _ConcreteReaper(reclaimed=False)

    async def _run() -> None:
        assert await reclaimed.reclaim_capture(capture) is True
        assert await declined.reclaim_capture(capture) is False

    asyncio.run(_run())
    assert reclaimed.seen == [capture]
    assert declined.seen == [capture]


def test_only_kinds_with_a_concrete_reaper_are_dispatchable() -> None:
    """A kind wired ``Null`` is unregistered for eligibility, not merely a no-op call."""
    concrete = _ConcreteReaper()

    kinds = dispatchable_capture_kinds(
        {"local-libvirt": NullCaptureReaper(), "remote-libvirt": concrete}
    )

    assert kinds == frozenset({"remote-libvirt"})
    assert concrete.seen == []


def test_an_all_null_registry_dispatches_no_kind() -> None:
    registry = {"local-libvirt": NullCaptureReaper(), "remote-libvirt": NullCaptureReaper()}

    assert dispatchable_capture_kinds(registry) == frozenset()
    assert set(registry) == {"local-libvirt", "remote-libvirt"}
