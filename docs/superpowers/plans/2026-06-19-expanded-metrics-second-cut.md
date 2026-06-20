# Expanded operational metrics (second cut) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit the deferred #601 metrics groups F (provider-op RED), G (build pipeline), H (capture/debug), I (job/queue health) within the ADR-0090 §4 label allowlist.

**Architecture:** Emit-only, additive instruments on the existing per-process aux `/metrics`. Each telemetry helper is built from a process-global OTel meter (`metrics.get_meter("kdive.<process>")`), exposes a `disabled()` no-op for un-instrumented runs, and is wired at the existing telemetry construction sites. No DB schema or migration change.

**Tech Stack:** Python 3.14, OpenTelemetry SDK, psycopg (async), pytest, `uv`/`ruff`/`ty`/`just`.

## Global Constraints

- ADR/spec: [ADR-0191](../../adr/0191-expanded-operational-metrics-second-cut.md), [spec](../../design/expanded-operational-metrics-second-cut.md). Settled decisions are not re-litigated.
- Label allowlist (`src/kdive/observability/labels.py` `ALLOWED_LABEL_KEYS`): a metric label key must be in the allowlist; the cardinality-guard test (`tests/observability/test_label_value_bounds.py`) must keep passing.
- Instrument names use the dotted convention `kdive.<area>.<name>`; the Prometheus renderer maps to `kdive_<area>_<name>` (histograms gain a `_seconds`/unit suffix).
- Line length 100; ruff lint set `E,F,I,UP,B,SIM`; `ty` strict. Absolute imports only. Google-style docstrings on public APIs. No banned doc words (critical/crucial/essential/significant/comprehensive/robust/elegant; "Sprint").
- Every new telemetry class mirrors the existing pattern in `src/kdive/jobs/worker_telemetry.py` / `src/kdive/reconciler/fleet.py`: built from a `Meter`, `_enabled` flag, classmethod `disabled()` via `cls.__new__(cls)`.
- Guardrails before each commit (run the ones touching changed files): `just lint`, `just type`, the focused `uv run python -m pytest <file> -q`. Full gate before push: `just ci`.
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/kdive/observability/labels.py` | add 4 allowlist keys | 1 |
| `src/kdive/domain/build_phase.py` (new) | `BuildPhase` enum | 1 |
| `src/kdive/jobs/provider_context.py` (new) | `provider_kind` contextvar | 2 |
| `src/kdive/providers/core/resolver.py` | `binding_for_system`/`binding_for_run` | 2 |
| `src/kdive/jobs/worker_telemetry.py` | F provider-op + I time-to-claim/retries instruments | 2, 8 |
| `src/kdive/jobs/handlers/*.py` | tag provider kind on provider-backed jobs | 2 |
| `src/kdive/jobs/worker.py` | record time-to-claim + retries | 8 |
| `src/kdive/jobs/build_telemetry.py` (new) | `BuildPhaseRecorder` | 3 |
| `src/kdive/providers/shared/build_host/orchestration.py` + `dispatch.py` | wrap build phases | 3 |
| `src/kdive/reconciler/build_host_fleet.py` (new) | `BuildHostSnapshot` + `BuildHostTelemetry` | 4 |
| `src/kdive/reconciler/loop.py` | refresh build-host snapshot per pass | 4 |
| `src/kdive/providers/ports/retrieve.py` | `CaptureOutput.raw_size_bytes` | 5 |
| `src/kdive/providers/*/retrieve*.py` + `fault_inject` | set `raw_size_bytes` | 5 |
| `src/kdive/jobs/handlers/capture_telemetry.py` (new) | `CaptureTelemetry` | 5 |
| `src/kdive/jobs/handlers/vmcore.py` | time + record capture | 5 |
| `src/kdive/reconciler/console_telemetry.py` (new) | `ConsoleTelemetry` | 6 |
| `src/kdive/providers/remote_libvirt/console/*` | report finalized bytes | 6 |
| `src/kdive/mcp/tools/debug/debug_session_telemetry.py` (new) | `DebugSessionTelemetry` | 7 |
| `src/kdive/mcp/tools/debug/sessions_lifecycle.py` | record at `end_session` | 7 |
| `src/kdive/reconciler/repairs/debug_sessions.py` | record reaped session duration | 7 |
| `src/kdive/__main__.py` | construct + inject the new telemetry per process | 2–8 |
| `tests/observability/test_label_value_bounds.py` | extend cardinality guard | 9 |

---

## Task 1: Labels foundation — allowlist keys + `BuildPhase` enum

**Files:**
- Create: `src/kdive/domain/build_phase.py`
- Modify: `src/kdive/observability/labels.py:19-39`
- Test: `tests/observability/test_label_allowlist.py`, `tests/domain/test_build_phase.py` (new)

**Interfaces:**
- Produces: `kdive.domain.build_phase.BuildPhase` (StrEnum, members `PROVISION="provision"`, `SOURCE_SYNC="source_sync"`, `CONFIGURE="configure"`, `COMPILE="compile"`, `MODULES="modules"`, `ARTIFACT="artifact"`); `ALLOWED_LABEL_KEYS` now contains `build_phase`, `capture_method`, `transport`, `build_host`.

- [ ] **Step 1: Write the failing test for `BuildPhase`**

```python
# tests/domain/test_build_phase.py
from __future__ import annotations

from kdive.domain.build_phase import BuildPhase


def test_build_phase_members_are_the_orchestrator_phases() -> None:
    assert {p.value for p in BuildPhase} == {
        "provision",
        "source_sync",
        "configure",
        "compile",
        "modules",
        "artifact",
    }
```

- [ ] **Step 2: Run it — expect failure** `uv run python -m pytest tests/domain/test_build_phase.py -q` → FAIL (module missing).

- [ ] **Step 3: Create the enum**

```python
# src/kdive/domain/build_phase.py
"""The build orchestrator's sub-phase vocabulary (ADR-0191 G1).

Bounds the ``build_phase`` metric label: a low-cardinality enum naming the distinct
timed stages of a kernel build (provision a build host, sync source, configure, compile,
install modules, extract artifacts). Never a per-object identifier.
"""

from __future__ import annotations

from enum import StrEnum


class BuildPhase(StrEnum):
    """A timed sub-phase of the build pipeline (ADR-0191 G1)."""

    PROVISION = "provision"
    SOURCE_SYNC = "source_sync"
    CONFIGURE = "configure"
    COMPILE = "compile"
    MODULES = "modules"
    ARTIFACT = "artifact"
```

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Extend the allowlist + its test.** Add the four keys to `ALLOWED_LABEL_KEYS` after the ADR-0190 block:

```python
        # ADR-0191 (#610 second cut): build sub-phase timings (build_phase), vmcore capture
        # method (capture_method), debug-session transport (transport), and the
        # deployment-bounded build-host fleet label (build_host) — bounded by the operator's
        # build_hosts table, the one non-enum allowlist key (ADR-0191 §1).
        "build_phase",
        "capture_method",
        "transport",
        "build_host",
```

Add to `tests/observability/test_label_allowlist.py` an assertion that these four keys are present (mirror the existing membership assertions for the ADR-0190 keys).

- [ ] **Step 6: Run** `just lint && just type && uv run python -m pytest tests/observability/test_label_allowlist.py tests/domain/test_build_phase.py -q` → PASS.

- [ ] **Step 7: Commit** `feat(observability): add second-cut metric label keys + BuildPhase`

---

## Task 2: Group F — provider-op RED

**Files:**
- Create: `src/kdive/jobs/provider_context.py`
- Modify: `src/kdive/providers/core/resolver.py` (add `binding_for_system`, `binding_for_run`), `src/kdive/jobs/worker_telemetry.py`, `src/kdive/jobs/handlers/{systems,runs_install,runs_boot,control,vmcore}.py` (+ `runs_build.py`), `src/kdive/__main__.py:459-462`
- Test: `tests/jobs/test_provider_context.py` (new), `tests/jobs/test_worker_telemetry.py`, `tests/providers/test_resolver.py`

**Interfaces:**
- Consumes: `ProviderBinding(kind: ResourceKind, runtime: ProviderRuntime)` (existing, `providers/core/resolver.py:57`).
- Produces: `set_provider_kind(value: str) -> None`, `clear_provider_kind() -> None`, `take_provider_kind() -> str | None` (`jobs/provider_context.py`); `ProviderResolver.binding_for_system(conn, system_id) -> ProviderBinding`, `binding_for_run(conn, run_id) -> ProviderBinding`; `WorkerTelemetry` now emits `kdive.provider.op.duration{provider,job_kind,outcome}` + `kdive.provider.op.errors{provider,job_kind}` when a provider kind was tagged for the job.

- [ ] **Step 1: Write the failing contextvar test**

```python
# tests/jobs/test_provider_context.py
from __future__ import annotations

from kdive.jobs.provider_context import clear_provider_kind, set_provider_kind, take_provider_kind


def test_set_then_take_returns_value_and_clears() -> None:
    set_provider_kind("local-libvirt")
    assert take_provider_kind() == "local-libvirt"
    assert take_provider_kind() is None  # take clears


def test_clear_resets() -> None:
    set_provider_kind("remote-libvirt")
    clear_provider_kind()
    assert take_provider_kind() is None
```

- [ ] **Step 2: Run** → FAIL (module missing).

- [ ] **Step 3: Create the contextvar**

```python
# src/kdive/jobs/provider_context.py
"""Per-job provider-kind tag for provider-op RED metrics (ADR-0191 F).

A provider-backed job handler tags the in-flight job with its provider kind where it
already resolves the runtime; the worker's per-job telemetry reads the tag on close to emit
``kdive.provider.op.*`` with a ``provider`` label. A contextvar (not a handler signature
change) carries the tag: the handler runs in the same task as the worker's job span, so a
value set in the handler is visible when the span closes. The worker clears it per job so a
provider-backed job never leaks its kind onto a following provider-less job.
"""

from __future__ import annotations

from contextvars import ContextVar

_provider_kind: ContextVar[str | None] = ContextVar("kdive_provider_kind", default=None)


def set_provider_kind(value: str) -> None:
    """Tag the current job with its provider kind (the ``provider`` label value)."""
    _provider_kind.set(value)


def clear_provider_kind() -> None:
    """Reset the tag (the worker calls this before each job dispatch)."""
    _provider_kind.set(None)


def take_provider_kind() -> str | None:
    """Return and clear the current job's provider kind (``None`` if untagged)."""
    value = _provider_kind.get()
    _provider_kind.set(None)
    return value
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Add resolver binding methods (failing test first).** In `tests/providers/test_resolver.py`, add a test mirroring the existing resolver tests that `binding_for_system` returns a `ProviderBinding` with the resolved kind and a `for_resource`-bound runtime (the existing `_Runtime` fake tracks `bound_to`). Seed the same `(kind, name)` row the existing tests use.

- [ ] **Step 6: Run** → FAIL (`binding_for_system` missing).

- [ ] **Step 7: Implement.** In `resolver.py`, after `runtime_for_run` (line ~143), add (mirroring `binding_for_session`):

```python
    async def binding_for_system(self, conn: AsyncConnection, system_id: UUID) -> ProviderBinding:
        kind, name = await self._kind_and_name(conn, _KIND_FOR_SYSTEM, system_id, "system")
        return ProviderBinding(kind=kind, runtime=self.resolve(kind).for_resource(name))

    async def binding_for_run(self, conn: AsyncConnection, run_id: UUID) -> ProviderBinding:
        kind, name = await self._kind_and_name(conn, _KIND_FOR_RUN, run_id, "run")
        return ProviderBinding(kind=kind, runtime=self.resolve(kind).for_resource(name))
```

Refactor `runtime_for_system`/`runtime_for_run` to delegate (`return (await self.binding_for_system(...)).runtime`) so there is one resolution path, matching how `runtime_for_session` delegates to `binding_for_session`.

- [ ] **Step 8: Run** `uv run python -m pytest tests/providers/test_resolver.py -q` → PASS.

- [ ] **Step 9: Add the provider-op instruments to `WorkerTelemetry` (failing test first).** In `tests/jobs/test_worker_telemetry.py`, add:

```python
def test_provider_op_duration_recorded_when_kind_tagged() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)
    from kdive.jobs.provider_context import set_provider_kind

    with telem.job_span("build") as span:
        set_provider_kind("local-libvirt")
        span.set_outcome("ok")
    points = _points_for(reader, "kdive.provider.op.duration")
    assert points, "provider-op duration not emitted for a tagged job"
    assert points[0].attributes["provider"] == "local-libvirt"
    assert points[0].attributes["job_kind"] == "build"


def test_provider_op_not_recorded_for_untagged_job_and_no_leak() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    tracer = TracerProvider().get_tracer("test")
    telem = WorkerTelemetry(tracer=tracer, meter=meter)
    from kdive.jobs.provider_context import set_provider_kind

    with telem.job_span("build") as span:  # tagged
        set_provider_kind("remote-libvirt")
        span.set_outcome("ok")
    with telem.job_span("teardown"):  # untagged — must NOT inherit remote-libvirt
        pass
    points = _points_for(reader, "kdive.provider.op.duration")
    kinds = {p.attributes["job_kind"] for p in points}
    assert "teardown" not in kinds
```

Add a `_points_for(reader, family_name)` helper that walks `reader.get_metrics_data()` and returns data points whose metric name matches (mirror `_all_points` in `tests/observability/test_label_value_bounds.py`).

- [ ] **Step 10: Run** → FAIL.

- [ ] **Step 11: Implement in `worker_telemetry.py`.** In `__init__`, add:

```python
        self._provider_op_duration: Histogram = meter.create_histogram(
            "kdive.provider.op.duration",
            unit="s",
            description="Provider-operation wall-clock duration, by provider and job kind.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._provider_op_errors: Counter = meter.create_counter(
            "kdive.provider.op.errors",
            unit="1",
            description="Failed provider operations, by provider and job kind.",
        )
```

In `job_span`, before opening the span (enabled branch), call `clear_provider_kind()` so a prior job's tag never leaks. In `_record`, after recording `kdive.job.duration`, add:

```python
        provider = take_provider_kind()
        if provider is not None:
            op_labels = {"provider": provider, "job_kind": handle.job_kind}
            self._provider_op_duration.record(elapsed, {**op_labels, "outcome": handle.outcome})
            if handle.outcome == "error":
                self._provider_op_errors.add(1, op_labels)
```

Import `clear_provider_kind`, `take_provider_kind` from `kdive.jobs.provider_context`. Note: when `disabled()`, `job_span` yields without touching the contextvar — call `take_provider_kind()` only in the enabled `_record`; a tag set by a handler in a disabled run is harmlessly cleared on the next job's `clear`. (Add a `clear_provider_kind()` at the top of the disabled `job_span` branch too, to be safe.)

- [ ] **Step 12: Run** `uv run python -m pytest tests/jobs/test_worker_telemetry.py -q` → PASS.

- [ ] **Step 13: Tag the handlers.** In each provider-backed handler, where it resolves the runtime, switch to the binding form and tag:
  - `jobs/handlers/systems.py` (provision/reprovision/teardown): `binding = await resolver.binding_for_system(conn, system_id); set_provider_kind(binding.kind.value)`, then use `binding.runtime`.
  - `jobs/handlers/runs_install.py`: same via `binding_for_system`.
  - `jobs/handlers/runs_boot.py`: via `binding_for_run`.
  - `jobs/handlers/control.py` (power/force_crash): via `binding_for_system`.
  - `jobs/handlers/vmcore.py`: via `binding_for_system` (also used by Task 5).
  - `jobs/handlers/runs_build.py`: the kind is `run.target_kind` already in hand — `set_provider_kind(run.target_kind.value)` (no binding needed).
  Import `set_provider_kind` from `kdive.jobs.provider_context` in each.

- [ ] **Step 14: Add a handler-to-metric integration assertion (the load-bearing link).** The contextvar tag is what connects a handler to the provider-op series; a handler that forgets `set_provider_kind` silently emits nothing with green unit tests. Add a test that drives one representative provider-backed handler through a fake resolver (whose `binding_for_system` returns a `ProviderBinding(kind=ResourceKind.LOCAL_LIBVIRT, runtime=<fake>)`) inside a `WorkerTelemetry.job_span` backed by an `InMemoryMetricReader`, then asserts `kdive.provider.op.duration` emitted with `provider="local-libvirt"`. Use a handler tagged within this task (step 13) — e.g. the provision handler with its provider call stubbed — not `capture_handler`, whose tag is added later in T5. This proves the handler actually tags; the manual-contextvar tests (steps 9-12) only prove the telemetry unit. Then run `uv run python -m pytest tests/jobs tests/mcp/lifecycle -q` → PASS.

- [ ] **Step 15: Verify __main__ needs no change** — `WorkerTelemetry` is already constructed at `__main__.py:459`; the new instruments ride on the same object. No wiring change for F.

- [ ] **Step 16: Run** `just lint && just type` → clean. **Commit** `feat(observability): emit provider-op RED metrics (ADR-0191 F)`

---

## Task 3: Group G1 — build sub-phase timings

**Files:**
- Create: `src/kdive/jobs/build_telemetry.py`
- Modify: `src/kdive/providers/shared/build_host/orchestration.py:88-105`, `src/kdive/providers/shared/build_host/dispatch.py` (the `factory()` provision boundary), the builder call chain that reaches `build_workspace` (`providers/{local_libvirt,remote_libvirt}/build.py`)
- Test: `tests/jobs/test_build_telemetry.py` (new)

**Interfaces:**
- Consumes: `BuildPhase` (Task 1).
- Produces: `BuildPhaseRecorder` with `phase(build_phase: BuildPhase, provider: str) -> contextmanager` and classmethod `disabled() -> BuildPhaseRecorder`; emits `kdive.build.phase.duration{build_phase,provider,outcome}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_build_telemetry.py
from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from kdive.domain.build_phase import BuildPhase
from kdive.jobs.build_telemetry import BuildPhaseRecorder


def _points(reader):
    data = reader.get_metrics_data()
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
    with pytest.raises(ValueError):  # noqa: PT011
        with rec.phase(BuildPhase.COMPILE, "remote-libvirt"):
            raise ValueError("boom")
    pts = _points(reader)
    assert pts and pts[0].attributes["outcome"] == "error"


def test_disabled_recorder_is_noop() -> None:
    reader = InMemoryMetricReader()
    rec = BuildPhaseRecorder.disabled()
    with rec.phase(BuildPhase.COMPILE, "local-libvirt"):
        pass
    assert not _points(reader)
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `BuildPhaseRecorder`**

```python
# src/kdive/jobs/build_telemetry.py
"""Build sub-phase duration recorder (ADR-0191 G1).

Times each delineated build stage (``BuildPhase``) and emits
``kdive.build.phase.duration{build_phase, provider, outcome}``. Built from the worker meter
and threaded into the shared build orchestrator. The build runs offloaded on a thread
(ADR-0181); ``Histogram.record`` is thread-safe, so the recorder is passed by value and used
inside the thread. ``disabled()`` is the no-op default for un-instrumented runs.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from kdive.domain.build_phase import BuildPhase

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter

_DURATION_BUCKETS = (0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0)


class BuildPhaseRecorder:
    """Record per-phase build durations (ADR-0191 G1)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.build.phase.duration",
            unit="s",
            description="Build sub-phase wall-clock duration, by phase and provider.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> BuildPhaseRecorder:
        """Return a no-op recorder (no meter) for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    @contextlib.contextmanager
    def phase(self, build_phase: BuildPhase, provider: str) -> Iterator[None]:
        """Time the wrapped block and record its duration with an ok/error outcome."""
        if not self._enabled:
            yield
            return
        started = time.perf_counter()
        outcome = "ok"
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            self._duration.record(
                time.perf_counter() - started,
                {"build_phase": build_phase.value, "provider": provider, "outcome": outcome},
            )
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Thread the recorder + provider kind into the orchestrator.** The central seam is `BuildHostOrchestrator.build_workspace(self, run_id, profile)` (`orchestration.py:88-105`), which delineates source-sync (`self.checkout`, line 98), configure (`run_olddefconfig`+`read_config`+`_validate_final_config`, lines 99-102), and compile (`run_make`, line 103). Change its signature to:

```python
    def build_workspace(
        self,
        run_id: UUID,
        profile: ServerBuildProfile,
        *,
        recorder: BuildPhaseRecorder = BuildPhaseRecorder.disabled(),
        provider: str = "",
    ) -> Path:
```

Wrap inside `build_workspace` (default-disabled recorder → non-build callers unaffected, `provider=""` only reached when recorder is disabled):
  - `self.checkout(...)` → `with recorder.phase(BuildPhase.SOURCE_SYNC, provider):`
  - `run_olddefconfig` + `read_config` + `_validate_final_config` → `with recorder.phase(BuildPhase.CONFIGURE, provider):`
  - `self.run_make(...)` → `with recorder.phase(BuildPhase.COMPILE, provider):`

  The `provision`, `modules`, and `artifact` phases live outside `build_workspace`:
  - **provision** — the transport/ephemeral-VM `factory()` bring-up in `dispatch.py` (the `with factory()` session entry; ADR-0181 offloads the whole session). Wrap that entry with `recorder.phase(BuildPhase.PROVISION, provider)`. Only the transport-backed providers (remote-libvirt / ephemeral-libvirt) enter `factory()`; local-libvirt builds in-process and has no provision phase, so it never wraps it.
  - **modules + artifact** — in the **remote** builder (`providers/remote_libvirt/build.py`), wrap `run_modules_install`+bundle as `MODULES` and the build-id/vmlinux extraction as `ARTIFACT`. The **local** builder (`providers/local_libvirt/build.py`) wraps only its build-id/objcopy extraction as `ARTIFACT` (it has no separate modules step).

  Per-provider phase map (what each provider emits): **local-libvirt** → {source_sync, configure, compile, artifact}; **remote-libvirt / ephemeral** → {provision, source_sync, configure, compile, modules, artifact}. Each builder threads `recorder=recorder, provider=ResourceKind.<X>.value` into its `build_workspace` call and into the `dispatch.py` provision wrap; the recorder is obtained from `RunHandlerPorts` (Step 6) and passed by value down to the offloaded thread (histogram `record` is thread-safe).

- [ ] **Step 6: Construct + inject via the run-handler registrar.** Build handlers are registered through `runs.register_handlers(registry, ports=runs.RunHandlerPorts(...))` (`mcp/app.py` `_register_run_handlers`). Add a `build_phase_recorder: BuildPhaseRecorder` field to `RunHandlerPorts`, built in `_register_run_handlers` as `BuildPhaseRecorder(meter=metrics.get_meter("kdive.worker"))` (the registrar pattern the allocations registrar uses at `registrar.py:44`; `from opentelemetry import metrics`). The build handler passes it (and `run.target_kind.value`) into the builder, which forwards to `build_workspace` and the `dispatch.py` provision wrap. No `__main__.py` change — the meter is process-global and no-ops until `init_telemetry`.

- [ ] **Step 7: Add an end-to-end emit assertion + run.** Add a test that drives a build through `BuildHostOrchestrator.build_workspace(run_id, profile, recorder=<reader-backed recorder>, provider="local-libvirt")` with stubbed `checkout`/`run_olddefconfig`/`read_config`/`run_make` seams (the orchestrator already takes these as injected callables, see `from_defaults` at `orchestration.py:55-78`) and asserts `source_sync`, `configure`, `compile` points are emitted with `provider="local-libvirt"`. Run `uv run python -m pytest tests/jobs/test_build_telemetry.py tests/providers -q` → PASS. `just lint && just type` → clean.

- [ ] **Step 8: Commit** `feat(observability): time build sub-phases (ADR-0191 G1)`

---

## Task 4: Group G2/G3 — build-host snapshot gauges

**Files:**
- Create: `src/kdive/reconciler/build_host_fleet.py`
- Modify: `src/kdive/reconciler/loop.py` (`_refresh_build_host_snapshot` + call in `_pass_loop`, `ReconcileConfig.build_host_telemetry`), `src/kdive/__main__.py:526-528`
- Test: `tests/reconciler/test_build_host_fleet.py` (new)

**Interfaces:**
- Produces: `BuildHostSnapshot(leases: Mapping[str,int], capacity: Mapping[str,int], reachable: Mapping[str,float])` with `empty()`; `async read_build_host_snapshot(conn) -> BuildHostSnapshot`; `BuildHostTelemetry(meter)` with `refresh(snapshot)`, `disabled()`, registering `kdive.build_host.leases{build_host}`, `kdive.build_host.capacity{build_host}`, `kdive.build_host.reachable{build_host}`.

- [ ] **Step 1: Write the failing snapshot DB test** (db-marked, mirrors `tests/reconciler` DB tests; seed `build_hosts` rows incl. a 0-lease host and an `unreachable` host, plus a `build_host_leases` row).

```python
# tests/reconciler/test_build_host_fleet.py (DB portion)
async def test_read_build_host_snapshot_counts_leases_capacity_reachable(db_conn) -> None:
    # seed two hosts: "alpha" ready max=4 with 1 lease, "beta" unreachable max=2 with 0 leases
    ...  # use the repo's build_hosts insert helper / raw INSERTs as other reconciler tests do
    snap = await read_build_host_snapshot(db_conn)
    assert snap.leases == {"alpha": 1, "beta": 0}
    assert snap.capacity == {"alpha": 4, "beta": 2}
    assert snap.reachable == {"alpha": 1.0, "beta": 0.0}
```

Plus a non-DB gauge-callback test mirroring `tests/reconciler` fleet tests: build `BuildHostTelemetry`, `refresh(BuildHostSnapshot(leases={"alpha":1}, capacity={"alpha":4}, reachable={"alpha":1.0}))`, collect the reader, assert the three families emit with `build_host="alpha"`; assert a second `refresh` swaps and a no-refresh keeps the last.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `build_host_fleet.py`** mirroring `reconciler/fleet.py` exactly (frozen dataclass, `read_*` with grouped queries, `*Telemetry` with observable gauges + cached frozen snapshot). Queries:

```python
# leases (LEFT JOIN so a 0-lease host still emits)
"SELECT h.name, count(l.run_id) FROM build_hosts h "
"LEFT JOIN build_host_leases l ON l.build_host_id = h.id GROUP BY h.name"
# capacity + reachable
"SELECT name, max_concurrent, state FROM build_hosts"
```

`reachable[name] = 1.0 if state == 'ready' else 0.0`. The gauge callbacks emit `Observation(n, {"build_host": name})` per host (copy `_capacity_callback` shape from `fleet.py:192-198`).

- [ ] **Step 4: Run** → PASS (DB test skips cleanly without Docker; runs in CI).

- [ ] **Step 5: Wire the reconciler.** In `loop.py`: import `BuildHostTelemetry, read_build_host_snapshot`; add `build_host_telemetry: BuildHostTelemetry | None = None` to `ReconcileConfig`; in `Reconciler.__init__` set `self._build_host_telemetry = config.build_host_telemetry or BuildHostTelemetry.disabled()`; add `_refresh_build_host_snapshot` mirroring `_refresh_fleet_snapshot` (best-effort, logs + keeps cache on failure); call it in `_pass_loop` right after `await self._refresh_fleet_snapshot()`.

- [ ] **Step 6: Construct in `__main__.py`** alongside `fleet_telemetry` (line ~526): `build_host_telemetry=BuildHostTelemetry(meter=telemetry.meter_provider.get_meter("kdive.reconciler"))`.

- [ ] **Step 7: Run** `uv run python -m pytest tests/reconciler/test_build_host_fleet.py -q && just lint && just type` → PASS/clean.

- [ ] **Step 8: Commit** `feat(observability): build-host lease/capacity/reachability gauges (ADR-0191 G2/G3)`

---

## Task 5: Group H1 — vmcore capture duration + bytes

**Files:**
- Create: `src/kdive/jobs/handlers/capture_telemetry.py`
- Modify: `src/kdive/providers/ports/retrieve.py:12-15` (`CaptureOutput`), all `CaptureOutput(...)` constructors (`providers/local_libvirt/retrieve.py`, `providers/remote_libvirt/retrieve/*.py`, `providers/fault_inject/*`), `src/kdive/jobs/handlers/vmcore.py`, `src/kdive/mcp/app.py` (vmcore registrar passes telemetry)
- Test: `tests/jobs/test_capture_telemetry.py` (new), `tests/mcp/lifecycle/test_vmcore_tools.py`

**Interfaces:**
- Consumes: `binding_for_system` (Task 2).
- Produces: `CaptureOutput` gains `raw_size_bytes: int`; `CaptureTelemetry(meter)` with `record(capture_method: str, provider: str, outcome: str, *, size_bytes: int | None = None)` + `disabled()`; emits `kdive.vmcore.capture.duration{capture_method,provider,outcome}` and `kdive.vmcore.capture.bytes{capture_method,provider}`.

- [ ] **Step 1: Add `raw_size_bytes` to `CaptureOutput` (failing test first).** Update `tests/providers` capture tests / `_capture_output()` builders to pass `raw_size_bytes=`; add an assertion in a provider retrieve test that `capture(...).raw_size_bytes == len(data)`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** `CaptureOutput`:

```python
class CaptureOutput(NamedTuple):
    raw: StoredArtifact
    redacted: StoredArtifact
    vmcore_build_id: str
    raw_size_bytes: int
```

In `local_libvirt/retrieve.py` `capture`, set `raw_size_bytes=len(data)` in the `CaptureOutput(...)` return. In the remote retrieve facade/kdump/host_dump constructors, set it from the captured size (`CoreInfo.size_bytes` for kdump; `len(data)` for host_dump). In `fault_inject`, set the synthetic size. Update every test that builds a `CaptureOutput`.

- [ ] **Step 4: Run** `uv run python -m pytest tests/providers -q` → PASS.

- [ ] **Step 5: Write the `CaptureTelemetry` failing test** (mirror Task 3's `_points` helper for both families; success records both families with the method/provider, bytes value == size; failure records duration `outcome=error` and **no** bytes point; `disabled()` no-op).

- [ ] **Step 6: Run** → FAIL.

- [ ] **Step 7: Implement `capture_telemetry.py`**

```python
# src/kdive/jobs/handlers/capture_telemetry.py
"""vmcore-capture duration + bytes telemetry (ADR-0191 H1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, Meter

_DURATION_BUCKETS = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0)
_BYTE_BUCKETS = (1e6, 1e7, 1e8, 5e8, 1e9, 5e9)


class CaptureTelemetry:
    """Record vmcore capture duration + raw byte size (ADR-0191 H1)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._duration: Histogram = meter.create_histogram(
            "kdive.vmcore.capture.duration",
            unit="s",
            description="vmcore capture wall-clock duration, by method and provider.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._bytes: Histogram = meter.create_histogram(
            "kdive.vmcore.capture.bytes",
            unit="By",
            description="Raw vmcore size captured, by method and provider.",
            explicit_bucket_boundaries_advisory=list(_BYTE_BUCKETS),
        )

    @classmethod
    def disabled(cls) -> CaptureTelemetry:
        """Return a no-op telemetry for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record(
        self,
        capture_method: str,
        provider: str,
        outcome: str,
        *,
        seconds: float,
        size_bytes: int | None = None,
    ) -> None:
        """Record a capture's duration, and its raw byte size on success."""
        if not self._enabled:
            return
        labels = {"capture_method": capture_method, "provider": provider}
        self._duration.record(seconds, {**labels, "outcome": outcome})
        if size_bytes is not None:
            self._bytes.record(size_bytes, labels)
```

(Adjust the `record` signature in the test to match: `record(method, provider, outcome, *, seconds=..., size_bytes=...)`.)

- [ ] **Step 8: Run** → PASS.

- [ ] **Step 9: Wire `capture_handler`.** Switch to `binding = await resolver.binding_for_system(conn, system_id)` (also tags F via `set_provider_kind(binding.kind.value)`), use `binding.runtime.retriever`. Wrap capture+finalize with `time.perf_counter()`; on success `telemetry.record(method.value, binding.kind.value, "ok", seconds=elapsed, size_bytes=output.raw_size_bytes)`; on exception record `outcome="error"` (no size) and re-raise. Add a `telemetry: CaptureTelemetry = CaptureTelemetry.disabled()` parameter to `capture_handler` and `register_handlers`; the registrar (`mcp/app.py` `_register_vmcore_handlers` / `vmcore.register_handlers`) builds `CaptureTelemetry(meter=metrics.get_meter("kdive.worker"))`.

- [ ] **Step 10: Run** `uv run python -m pytest tests/mcp/lifecycle/test_vmcore_tools.py tests/jobs/test_capture_telemetry.py -q` → PASS. `just lint && just type` → clean.

- [ ] **Step 11: Commit** `feat(observability): vmcore capture duration + bytes (ADR-0191 H1)`

---

## Task 6: Group H2 — finalized console bytes

**Files:**
- Create: `src/kdive/reconciler/console_telemetry.py`
- Modify: the remote console finalize path (`providers/remote_libvirt/console/collector.py` `finalize` / `wiring.py` `write_console_artifact`), `src/kdive/__main__.py` (console hosting construction)
- Test: `tests/reconciler/test_console_telemetry.py` (new), `tests/providers/remote_libvirt/console/test_console_collector.py`

**Interfaces:**
- Produces: `ConsoleTelemetry(meter)` with `record(byte_len: int)` + `disabled()`; emits `kdive.console.bytes{outcome}` (`outcome ∈ {success, empty}`).

- [ ] **Step 1: Failing test** (mirror Task 3 `_points`): `record(5)` adds 5 under `outcome=success`; `record(0)` emits an `outcome=empty` point with value 0; `disabled()` no-op; no `system`/identifier attribute on any point.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `console_telemetry.py`**

```python
# src/kdive/reconciler/console_telemetry.py
"""Finalized console-bytes counter (ADR-0191 H2).

Aggregate total console bytes finalized — no per-System label (ADR-0090 §4). ``outcome``
splits a content-bearing finalize (``success``) from an empty one (``empty``) so a 0-byte
console (the #594 failure shape) stays visible. Remote-libvirt console scope (ADR-0191 H2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Meter


class ConsoleTelemetry:
    """Count finalized console bytes (ADR-0191 H2)."""

    def __init__(self, *, meter: Meter) -> None:
        self._enabled = True
        self._bytes: Counter = meter.create_counter(
            "kdive.console.bytes",
            unit="By",
            description="Console bytes finalized (remote-libvirt), by content outcome.",
        )

    @classmethod
    def disabled(cls) -> ConsoleTelemetry:
        """Return a no-op telemetry for tests or an un-instrumented run."""
        instance = cls.__new__(cls)
        instance._enabled = False
        return instance

    def record(self, byte_len: int) -> None:
        """Add ``byte_len`` under ``success``, or mark an empty finalize under ``empty``."""
        if not self._enabled:
            return
        if byte_len > 0:
            self._bytes.add(byte_len, {"outcome": "success"})
        else:
            self._bytes.add(0, {"outcome": "empty"})
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Wire the finalize seam.** Read `collector.py` `finalize` (it assembles `bytes(assembled)` and calls `write_console_artifact`). Pass a `ConsoleTelemetry` into the collector (factory in the reconciler console hosting); at finalize, after the artifact write, call `telemetry.record(len(assembled))`. Default `ConsoleTelemetry.disabled()` so existing collector tests are unaffected; add a collector test asserting `record` is called with the assembled length.

- [ ] **Step 6: Construct in `__main__.py`** where console hosting is built: `ConsoleTelemetry(meter=telemetry.meter_provider.get_meter("kdive.reconciler"))`, threaded into the collector factory.

- [ ] **Step 7: Run** `uv run python -m pytest tests/reconciler/test_console_telemetry.py tests/providers/remote_libvirt/console -q && just lint && just type` → PASS/clean.

- [ ] **Step 8: Commit** `feat(observability): finalized console-bytes counter (ADR-0191 H2)`

---

## Task 7: Group H3 — debug-session duration

**Files:**
- Create: `src/kdive/mcp/tools/debug/debug_session_telemetry.py`
- Modify: `src/kdive/mcp/tools/debug/sessions_lifecycle.py` (`end_session` + registrar), `src/kdive/reconciler/repairs/debug_sessions.py` (RETURNING `created_at` + record), `src/kdive/reconciler/loop.py` (pass telemetry to the repair), `src/kdive/__main__.py`
- Test: `tests/mcp/debug/test_debug_session_telemetry.py` (new), `tests/reconciler/test_debug_session_repair.py` (or existing)

**Interfaces:**
- Produces: `DebugSessionTelemetry(meter)` with `record(transport: str, outcome: str, seconds: float)` + `disabled()`; emits `kdive.debug.session.duration{transport,outcome}` (`outcome ∈ {ok, error, reaped}`).

- [ ] **Step 1: Failing test** for `DebugSessionTelemetry` (mirror Task 3 `_points`): `record("gdbstub","ok",12.0)` emits one point with those attrs; `disabled()` no-op.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `debug_session_telemetry.py`** (one histogram, buckets `(1.0, 10.0, 60.0, 300.0, 1800.0, 3600.0, 14400.0)`, `record(transport, outcome, seconds)`, `disabled()`).

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Record at `end_session` (server).** Compute `(now - session.created_at).total_seconds()`; record `(session.transport, outcome, seconds)` where outcome is `ok`/`error` from the detach result. Inject `DebugSessionTelemetry(meter=metrics.get_meter("kdive.mcp"))` via the debug-session registrar. Add a handler test asserting a duration point is emitted on `end_session`.

- [ ] **Step 6: Record at the reaper (reconciler).** In `repair_dead_sessions`, add `created_at` to the `RETURNING` list; for each reaped row, `telemetry.record(row["transport"], "reaped", (now - row["created_at"]).total_seconds())`. Add a `telemetry: DebugSessionTelemetry = DebugSessionTelemetry.disabled()` parameter; thread the real one through `ReconcileConfig` → the `dead_sessions` repair closure (`loop.py:259-263`). Use the DB clock-consistent `now` (read `now()` in the same statement or compute from `datetime.now(tz=UTC)`; prefer returning `now() - created_at` as `age_seconds` from the UPDATE to avoid clock skew). Add a reconciler test asserting a `reaped` duration point.

- [ ] **Step 7: Construct in `__main__.py`** one `DebugSessionTelemetry` per process: the server registrar uses `metrics.get_meter("kdive.mcp")`; the reconciler config gets `DebugSessionTelemetry(meter=telemetry.meter_provider.get_meter("kdive.reconciler"))`.

- [ ] **Step 8: Run** `uv run python -m pytest tests/mcp/debug tests/reconciler -q && just lint && just type` → PASS/clean.

- [ ] **Step 9: Commit** `feat(observability): debug-session duration, clean + reaped (ADR-0191 H3)`

---

## Task 8: Group I — job time-to-claim + retries

**Files:**
- Modify: `src/kdive/jobs/worker_telemetry.py`, `src/kdive/jobs/worker.py` (`run_once` + the two `queue.fail` sites)
- Test: `tests/jobs/test_worker_telemetry.py`, `tests/jobs/test_worker.py`

**Interfaces:**
- Produces: `WorkerTelemetry.record_time_to_claim(job_kind: str, seconds: float)`, `record_job_retry(job_kind: str)`; emits `kdive.job.time_to_claim{job_kind}`, `kdive.job.retries{job_kind}`.

- [ ] **Step 1: Failing telemetry tests** (`_points`): `record_time_to_claim("build", 3.0)` emits one point with `job_kind="build"`; `record_job_retry("build")` increments the retries counter; both no-op when disabled.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** Add to `WorkerTelemetry.__init__`:

```python
        self._time_to_claim: Histogram = meter.create_histogram(
            "kdive.job.time_to_claim",
            unit="s",
            description="Queue latency from enqueue to claim, by job kind.",
            explicit_bucket_boundaries_advisory=list(_DURATION_BUCKETS),
        )
        self._retries: Counter = meter.create_counter(
            "kdive.job.retries",
            unit="1",
            description="Job requeues (non-terminal failures), by job kind.",
        )
```

```python
    def record_time_to_claim(self, job_kind: str, seconds: float) -> None:
        """Record enqueue→claim latency (no-op when disabled or seconds < 0)."""
        if self._enabled and seconds >= 0.0:
            self._time_to_claim.record(seconds, {"job_kind": job_kind})

    def record_job_retry(self, job_kind: str) -> None:
        """Count one non-terminal requeue (no-op when disabled)."""
        if self._enabled:
            self._retries.add(1, {"job_kind": job_kind})
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Wire `worker.py`.** In `run_once`, after a non-None `dequeue` and inside the telemetry-enabled branch, if `job.heartbeat_at` and `job.created_at` are set: `self._telemetry.record_time_to_claim(job.kind.value, (job.heartbeat_at - job.created_at).total_seconds())`. In `_run_handler`'s exception path, after `failed_job = await queue.fail(...)`, if `failed_job.state is JobState.QUEUED`: `self._telemetry.record_job_retry(job.kind.value)`. (The no-handler path is always `terminal=True` → never a retry; leave it.)

- [ ] **Step 6: Failing worker test then verify.** Add a `tests/jobs/test_worker.py` test that a handler raising a non-terminal `CategorizedError` records a retry, and a terminal one does not. Drive through the existing worker test harness (in-memory queue + injected telemetry double or an `InMemoryMetricReader`-backed `WorkerTelemetry`).

- [ ] **Step 7: Run** `uv run python -m pytest tests/jobs/test_worker.py tests/jobs/test_worker_telemetry.py -q && just lint && just type` → PASS/clean.

- [ ] **Step 8: Commit** `feat(observability): job time-to-claim + retries (ADR-0191 I)`

---

## Task 9: Cardinality guard + end-to-end render

**Files:**
- Modify: `tests/observability/test_label_value_bounds.py`
- Test: same file

**Interfaces:**
- Consumes: every telemetry class from Tasks 2–8.

- [ ] **Step 1: Extend `_emit_everything`** to drive each new emitter into the shared in-memory meter: a tagged `WorkerTelemetry.job_span` (F + I via `record_time_to_claim`/`record_job_retry`), `BuildPhaseRecorder.phase`, `BuildHostTelemetry.refresh` with a seeded snapshot, `CaptureTelemetry.record`, `ConsoleTelemetry.record`, `DebugSessionTelemetry.record` (incl. `outcome="reaped"`).

- [ ] **Step 2: Extend `_BOUNDS`** with `build_phase` → `{p.value for p in BuildPhase}`, `capture_method` → `{c.value for c in CaptureMethod}`, `transport` → `{"gdbstub", "drgn-live"}`. Add `build_host` handling: assert emitted `build_host` values ⊆ the seeded host-name set used in `_emit_everything`, with a comment that `build_host` is the documented deployment-bounded (non-enum) exception (ADR-0191 §1).

- [ ] **Step 3: Extend the no-identifier-leak assertion** so the four new keys are covered (they already are by the allowlist check; assert no `system`/`system_id` key appears on any console/capture/debug point).

- [ ] **Step 4: Extend the Prometheus-render assertion** family list with `kdive_provider_op_duration`, `kdive_build_phase_duration`, `kdive_build_host_leases`, `kdive_build_host_reachable`, `kdive_vmcore_capture_duration`, `kdive_console_bytes`, `kdive_debug_session_duration`, `kdive_job_time_to_claim`, `kdive_job_retries`.

- [ ] **Step 5: Run** `uv run python -m pytest tests/observability/test_label_value_bounds.py -v` → PASS.

- [ ] **Step 6: Full gate** `just ci` → green. **Commit** `test(observability): cardinality guard for the second-cut metrics (ADR-0191)`

---

## Self-review notes

- **Spec coverage:** F→T2, G1→T3, G2/G3→T4, H1→T5, H2→T6, H3→T7, I→T8, labels→T1, cardinality+render→T9, lease-reclaim overlap→documented (no task, by design).
- **Type consistency:** `CaptureTelemetry.record(method, provider, outcome, *, seconds, size_bytes)` is used identically in T5 step 7/9 and T9; `BuildPhaseRecorder.phase(BuildPhase, str)` consistent T3/T9; `provider_context` set/take/clear consistent T2/T9.
- **Ordering:** T2 produces `binding_for_system` consumed by T5; do T2 before T5. T1 (labels/enum) is a prerequisite for all. T3 build wiring depends on no other task. T9 last (consumes all). T4/T6/T7/T8 are mutually independent after T1.
