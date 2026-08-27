"""Catalog, report, and composition wiring for the orphaned-capture sweep (ADR-0556, #1946)."""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import timedelta
from typing import cast

from kdive.providers.assembly.composition import ProviderComposition
from kdive.providers.infra.reaping import (
    NullCaptureReaper,
    NullReaper,
    dispatchable_capture_kinds,
)
from kdive.providers.local_libvirt.reaping import LocalLibvirtCaptureReaper
from kdive.providers.remote_libvirt.reaping.capture import RemoteLibvirtCaptureReaper
from kdive.reconciler import loop
from kdive.reconciler.cleanup import provider_reaping
from kdive.reconciler.loop import ReconcileConfig, ReconcileReport
from tests.reconcile_helpers import make_reconcile_config

_CAPTURE_KINDS = {"local-libvirt", "remote-libvirt"}


def _plan_names(config: ReconcileConfig) -> tuple[str, ...]:
    return tuple(
        spec.name
        for spec in loop._repair_plan(
            reaper=NullReaper(), config=config, image_publish_grace=timedelta(seconds=1)
        )
    )


def test_the_capture_sweep_is_a_named_catalog_repair_with_a_report_field() -> None:
    names = _plan_names(make_reconcile_config())

    assert "reaped_captures" in loop.ALL_REPAIR_KINDS
    assert "reaped_captures" in names
    assert "reaped_captures" in {field.name for field in fields(ReconcileReport)}
    assert "reaped_captures" in {entry.report_field for entry in loop._REPAIR_CATALOG}


def test_the_capture_sweep_runs_after_the_abandoned_job_repair() -> None:
    """repair_abandoned_jobs is what dead-letters a lease-lapsed capture into a terminal state."""
    names = _plan_names(make_reconcile_config())

    assert names.index("abandoned_jobs") < names.index("reaped_captures")


def test_the_report_counts_the_captures_a_pass_reclaimed() -> None:
    report = ReconcileReport.from_counts({"reaped_captures": 3}, [])

    assert report.reaped_captures == 3
    assert report.repair_counts["reaped_captures"] == 3


def test_the_default_config_registers_no_capture_reaper_at_all() -> None:
    """A deployment that wires nothing must not reap; the default cannot be an active port."""
    assert dict(make_reconcile_config().capture_reapers) == {}


def test_the_sweep_is_wired_with_its_configured_pacing_values(migrated_url: str) -> None:
    """The catalog entry must thread the operator's settle/batch/backoff, not re-derive defaults."""
    seen: dict[str, object] = {}

    async def _spy(conn: object, reapers: object, **kwargs: object) -> int:
        seen.update(kwargs)
        seen["reapers"] = reapers
        return 0

    config = make_reconcile_config(
        capture_reapers={"remote-libvirt": NullCaptureReaper()},
        capture_settle=timedelta(minutes=7),
        capture_reap_batch=3,
        capture_retry_base=timedelta(minutes=2),
        capture_retry_cap=timedelta(hours=9),
    )
    original = loop._reap_orphaned_captures
    loop._reap_orphaned_captures = cast(object, _spy)  # ty: ignore[invalid-assignment]
    try:
        plan = loop._repair_plan(
            reaper=NullReaper(), config=config, image_publish_grace=timedelta(seconds=1)
        )
        repair = next(spec.repair for spec in plan if spec.name == "reaped_captures")
        assert asyncio.run(repair(cast(object, None))) == 0  # ty: ignore[invalid-argument-type]
    finally:
        loop._reap_orphaned_captures = original

    assert seen["settle"] == timedelta(minutes=7)
    assert seen["batch"] == 3
    assert seen["retry_base"] == timedelta(minutes=2)
    assert seen["retry_cap"] == timedelta(hours=9)
    assert set(cast(dict, seen["reapers"])) == {"remote-libvirt"}


def test_both_capture_kinds_are_wired_concrete() -> None:
    """ADR-0556/0567: #1947 registered remote's reaper; #1948 registers local's."""
    composition = ProviderComposition()

    reapers = composition.build_reconciler_capture_reapers(
        enable_local_libvirt=True, enable_remote_libvirt=True, enable_fault_inject=True
    )

    assert set(reapers) == _CAPTURE_KINDS
    assert isinstance(reapers["local-libvirt"], LocalLibvirtCaptureReaper)
    assert isinstance(reapers["remote-libvirt"], RemoteLibvirtCaptureReaper)
    assert dispatchable_capture_kinds(reapers) == _CAPTURE_KINDS


def test_a_disabled_provider_contributes_no_capture_reaper() -> None:
    composition = ProviderComposition()

    only_local = composition.build_reconciler_capture_reapers(
        enable_local_libvirt=True, enable_remote_libvirt=False, enable_fault_inject=False
    )
    neither = composition.build_reconciler_capture_reapers(
        enable_local_libvirt=False, enable_remote_libvirt=False, enable_fault_inject=False
    )

    assert set(only_local) == {"local-libvirt"}
    assert dict(neither) == {}


def test_the_pacing_defaults_are_the_documented_ones() -> None:
    assert timedelta(minutes=30) == provider_reaping.DEFAULT_CAPTURE_SETTLE
    assert provider_reaping.DEFAULT_CAPTURE_REAP_BATCH == 25
    assert timedelta(minutes=5) == provider_reaping.DEFAULT_CAPTURE_RETRY_BASE
    assert timedelta(hours=6) == provider_reaping.DEFAULT_CAPTURE_RETRY_CAP
    assert loop.DEFAULT_CAPTURE_SETTLE is provider_reaping.DEFAULT_CAPTURE_SETTLE


def test_the_on_demand_reconcile_pass_runs_the_same_capture_lane() -> None:
    """A lane the periodic loop runs and ops.reconcile_now cannot is drift #1947 would inherit."""
    from kdive.mcp.assembly.tool_registration import AppAssembly
    from kdive.mcp.tools.ops.reconcile.reconcile import ReconcileRepairPorts

    assert "capture_reapers" in {field.name for field in fields(AppAssembly)}
    defaults = {field.name: field.default for field in fields(ReconcileRepairPorts)}
    assert "capture_reapers" in defaults
    assert dict(cast(dict, defaults["capture_reapers"])) == {}

    ports = ReconcileRepairPorts(
        reaper=NullReaper(),
        upload_store=cast(loop.ReconcileUploadStore, object()),
        image_store=cast(loop.ImageSweepStore, object()),
        capture_reapers={"remote-libvirt": NullCaptureReaper()},
    )
    assert set(ports.capture_reapers) == {"remote-libvirt"}
