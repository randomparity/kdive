"""Tests for BuildPhaseRecorder (ADR-0191 G1)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.domain.build_phase import BuildPhase
from kdive.jobs.build_telemetry import BuildPhaseRecorder
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.execution import CapturedStep
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator


def _points(reader: InMemoryMetricReader) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "kdive.build.phase.duration":
                    out.extend(m.data.data_points)
    return out


def test_phase_records_ok_on_clean_block() -> None:
    reader = InMemoryMetricReader()
    rec = BuildPhaseRecorder(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    with rec.phase(BuildPhase.COMPILE, "local-libvirt"):
        pass
    pts = _points(reader)
    assert pts and pts[0].attributes["build_phase"] == "compile"
    assert pts[0].attributes["provider"] == "local-libvirt"
    assert pts[0].attributes["outcome"] == "ok"


def test_phase_records_error_when_block_raises() -> None:
    reader = InMemoryMetricReader()
    rec = BuildPhaseRecorder(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    with pytest.raises(ValueError), rec.phase(BuildPhase.COMPILE, "remote-libvirt"):  # noqa: PT011
        raise ValueError("boom")
    pts = _points(reader)
    assert pts and pts[0].attributes["outcome"] == "error"


def test_disabled_recorder_is_noop() -> None:
    reader = InMemoryMetricReader()
    rec = BuildPhaseRecorder.disabled()
    with rec.phase(BuildPhase.COMPILE, "local-libvirt"):
        pass
    assert not _points(reader)


# --- End-to-end: recorder threaded through the build orchestrator ---

_GOOD_CONFIG = "\n".join(
    ["CONFIG_CRASH_DUMP=y", "CONFIG_DEBUG_INFO=y", "CONFIG_DEBUG_INFO_DWARF5=y"]
)
_FRAGMENT_BYTES = _GOOD_CONFIG.encode()
_RUN = UUID("11111111-1111-1111-1111-111111111111")


def _profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _orchestrator(tmp_path: Path) -> BuildHostOrchestrator:
    """An orchestrator with inert build seams; all steps succeed with no side effects."""
    return BuildHostOrchestrator.create(
        workspace_root=tmp_path / "ws",
        catalog_fetch=lambda _name: _FRAGMENT_BYTES,
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: CapturedStep(0, ""),
        read_config=lambda _w: _GOOD_CONFIG,
        run_make=lambda _w: CapturedStep(0, ""),
    )


def test_build_workspace_emits_source_sync_configure_compile(tmp_path: Path) -> None:
    reader = InMemoryMetricReader()
    recorder = BuildPhaseRecorder(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    orch = _orchestrator(tmp_path)

    orch.build_workspace(_RUN, _profile(), recorder=recorder, provider="local-libvirt")

    pts = _points(reader)
    phases_emitted = {p.attributes["build_phase"] for p in pts}
    assert BuildPhase.SOURCE_SYNC.value in phases_emitted
    assert BuildPhase.CONFIGURE.value in phases_emitted
    assert BuildPhase.COMPILE.value in phases_emitted
    for pt in pts:
        assert pt.attributes["provider"] == "local-libvirt"
        assert pt.attributes["outcome"] == "ok"
