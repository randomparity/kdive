"""Tests for the reconciler loop (ADR-0021, issue #12)."""

from __future__ import annotations

import asyncio

from kdive.reconciler.loop import InfraReaper, NullReaper, ReconcileReport


def test_null_reaper_is_an_infra_reaper() -> None:
    assert isinstance(NullReaper(), InfraReaper)


def test_null_reaper_lists_nothing_and_destroy_is_noop() -> None:
    async def _run() -> None:
        reaper = NullReaper()
        assert await reaper.list_owned() == []
        assert await reaper.destroy("anything") is None

    asyncio.run(_run())


def test_reconcile_report_holds_counts_and_failures() -> None:
    report = ReconcileReport(
        orphaned_systems=1,
        abandoned_jobs=2,
        dead_sessions=3,
        leaked_domains=4,
        failures=("abandoned_jobs",),
    )
    assert report.orphaned_systems == 1
    assert report.failures == ("abandoned_jobs",)
