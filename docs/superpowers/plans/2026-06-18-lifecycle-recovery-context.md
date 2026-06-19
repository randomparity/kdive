# Lifecycle Recovery Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add redaction-safe recovery context (parent ids, placement, profile summary, lease/lifecycle timing, artifact refs) to the `allocations`/`systems`/`runs` `get`/`list` MCP envelopes so an agent can resume a workflow from read tools alone (#568).

**Architecture:** Additive only — new keys in the existing `ToolResponse.data` and artifact pointers in `refs`. A shared `_recovery.py` helper extracts an allowlisted profile summary (never a free-form ref string). No schema change (ADR-0113 flat outputSchema), no migration, no new tool. Implements [ADR-0180](../../adr/0180-lifecycle-recovery-context.md); spec at [docs/specs/2026-06-18-lifecycle-recovery-context.md](../../specs/2026-06-18-lifecycle-recovery-context.md).

**Tech Stack:** Python 3.14, `uv`, `psycopg`/`psycopg_pool`, pydantic, `pytest` (testcontainers Postgres via the `migrated_url` fixture), `ruff`, `ty`.

## Global Constraints

- Guardrails before every commit: `just lint`, `just type` (whole tree), and the focused tests. Full `just ci` before the first push.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, src + tests).
- Absolute imports only (`kdive...`), no relative imports.
- Redaction allowlist (load-bearing): a summary echoes only enumerated discriminators (`source`, `boot_method`, `arch`, provenance), registry identifiers (`build_host`, `shape`), object ids, object-store artifact keys (`kernel`/`debuginfo`), sizing ints, queue counters, and ISO timestamps. **Never** a free-form profile reference string (`kernel_source_ref`/remote/ref, `patch_ref`, rootfs refs, `ssh_credential_ref`, `base_image_volume`, `domain_xml_params`, `crashkernel`).
- Read profile fields with `.get()` — never re-parse the stored profile (a read tool must not raise on a slightly-off stored document).
- Timestamps are `datetime.isoformat()` strings (psycopg returns timezone-aware datetimes from `timestamptz`).
- Doc-style: plain prose; no "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- Every commit ends with the trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Shared recovery helpers + `ToolResponse.failure(refs=...)`

**Files:**
- Create: `src/kdive/mcp/tools/lifecycle/_recovery.py`
- Modify: `src/kdive/mcp/responses.py` (the `failure` classmethod)
- Test: `tests/mcp/lifecycle/test_recovery_helpers.py` (create), `tests/mcp/core/test_responses.py` (extend)

**Interfaces:**
- Produces:
  - `iso(dt: datetime | None) -> str | None`
  - `build_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]` — keys `build_source` (always), `build_host` (when a str), `build_source_provenance` (`git`/`warm-tree`/`external`).
  - `provisioning_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]` — keys among `arch`, `boot_method` (str values) and `vcpu`, `memory_mb`, `disk_gb` (int values), each included only when present with the right type.
  - `ToolResponse.failure(..., refs: dict[str, str] | None = None, ...)` — additive optional param threaded into the envelope `refs`.

- [ ] **Step 1: Write the failing helper tests**

Create `tests/mcp/lifecycle/test_recovery_helpers.py`:

```python
"""Unit tests for the redaction-safe recovery summary helpers (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime

from kdive.mcp.tools.lifecycle._recovery import (
    build_profile_summary,
    iso,
    provisioning_profile_summary,
)


def test_iso_serializes_and_passes_through_none() -> None:
    dt = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    assert iso(dt) == dt.isoformat()
    assert iso(None) is None


def test_build_summary_git_provenance_omits_remote() -> None:
    profile = {
        "source": "server",
        "build_host": "build-1",
        "kernel_source_ref": {"git": {"remote": "https://h/r", "ref": "main"}},
    }
    summary = build_profile_summary(profile)
    assert summary == {
        "build_source": "server",
        "build_host": "build-1",
        "build_source_provenance": "git",
    }
    assert "h/r" not in str(summary)


def test_build_summary_warm_tree_and_default_source() -> None:
    summary = build_profile_summary({"kernel_source_ref": "REPLACE_ME-warm-tree"})
    assert summary["build_source"] == "server"
    assert summary["build_source_provenance"] == "warm-tree"
    assert "build_host" not in summary


def test_build_summary_external_provenance() -> None:
    summary = build_profile_summary({"source": "external"})
    assert summary["build_source"] == "external"
    assert summary["build_source_provenance"] == "external"


def test_provisioning_summary_allowlists_only_safe_fields() -> None:
    profile = {
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "vcpu": 4,
        "memory_mb": 8192,
        "disk_gb": 40,
        "kernel_source_ref": "git@secret",
        "provider": {"local-libvirt": {"ssh_credential_ref": "file:///run/secret"}},
    }
    summary = provisioning_profile_summary(profile)
    assert summary == {
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "vcpu": 4,
        "memory_mb": 8192,
        "disk_gb": 40,
    }
    assert "secret" not in str(summary)


def test_provisioning_summary_tolerates_missing_and_wrong_types() -> None:
    # A slightly-off stored document must not raise and must drop bad-typed values.
    assert provisioning_profile_summary({}) == {}
    assert provisioning_profile_summary({"vcpu": "lots", "arch": 7}) == {}
```

- [ ] **Step 2: Run the helper tests — expect failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_recovery_helpers.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.mcp.tools.lifecycle._recovery`.

- [ ] **Step 3: Implement `_recovery.py`**

Create `src/kdive/mcp/tools/lifecycle/_recovery.py`:

```python
"""Redaction-safe recovery summaries for lifecycle get/list envelopes (#568, ADR-0180).

These helpers extract an allowlisted summary from a stored profile document. They read
fields with ``.get()`` (never re-parse the profile) so a read tool cannot raise on a
slightly-off stored document, and they echo only enumerated discriminators, registry
identifiers, and sizing integers — never a free-form reference string that could carry an
inline credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from kdive.serialization import JsonValue


def iso(dt: datetime | None) -> str | None:
    """Serialize a datetime to ISO-8601, passing ``None`` through."""
    return dt.isoformat() if dt is not None else None


def _provenance(source: str, kernel_source_ref: object) -> str:
    """Derive the source provenance label without echoing the reference itself."""
    if source == "external":
        return "external"
    if isinstance(kernel_source_ref, Mapping) and "git" in kernel_source_ref:
        return "git"
    return "warm-tree"


def build_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return the allowlisted build summary: source lane, host, and derived provenance."""
    raw_source = profile.get("source", "server")
    source = raw_source if isinstance(raw_source, str) else "server"
    summary: dict[str, JsonValue] = {"build_source": source}
    host = profile.get("build_host")
    if isinstance(host, str):
        summary["build_host"] = host
    summary["build_source_provenance"] = _provenance(source, profile.get("kernel_source_ref"))
    return summary


def provisioning_profile_summary(profile: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return the allowlisted provisioning summary: arch, boot method, and sizing."""
    summary: dict[str, JsonValue] = {}
    for key in ("arch", "boot_method"):
        value = profile.get(key)
        if isinstance(value, str):
            summary[key] = value
    for key in ("vcpu", "memory_mb", "disk_gb"):
        value = profile.get(key)
        # bool is an int subclass; exclude it explicitly so a stray bool isn't surfaced.
        if isinstance(value, int) and not isinstance(value, bool):
            summary[key] = value
    return summary
```

- [ ] **Step 4: Run the helper tests — expect pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_recovery_helpers.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Write the failing `failure(refs=...)` test**

Append to `tests/mcp/core/test_responses.py`:

```python
def test_failure_carries_optional_refs() -> None:
    from kdive.domain.errors import ErrorCategory
    from kdive.mcp.responses import ToolResponse

    resp = ToolResponse.failure(
        "run-1", ErrorCategory.INSTALL_FAILURE, refs={"kernel": "s3://b/k"}
    )
    assert resp.refs == {"kernel": "s3://b/k"}
    assert resp.error_category == ErrorCategory.INSTALL_FAILURE.value
    assert resp.retryable is False  # category-iff-failure invariant still holds


def test_failure_refs_default_empty() -> None:
    from kdive.domain.errors import ErrorCategory
    from kdive.mcp.responses import ToolResponse

    resp = ToolResponse.failure("run-1", ErrorCategory.INSTALL_FAILURE)
    assert resp.refs == {}
```

(Use the import style already present at the top of `test_responses.py`; inline imports
shown here only for self-containment — move them to the module header if that is the file's
convention.)

- [ ] **Step 6: Run the responses test — expect failure**

Run: `uv run python -m pytest tests/mcp/core/test_responses.py::test_failure_carries_optional_refs -q`
Expected: FAIL — `TypeError: failure() got an unexpected keyword argument 'refs'`.

- [ ] **Step 7: Add `refs` to `ToolResponse.failure`**

In `src/kdive/mcp/responses.py`, change the `failure` classmethod signature and body:

```python
    @classmethod
    def failure(
        cls,
        object_id: str,
        category: ErrorCategory,
        *,
        detail: str | None = None,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseDataInput | None = None,
    ) -> ToolResponse:
        return cls(
            object_id=object_id,
            status="error",
            error_category=category.value,
            detail=suppressed_detail(category, detail),
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=dict(data or {}),
        )
```

- [ ] **Step 8: Run the responses tests — expect pass**

Run: `uv run python -m pytest tests/mcp/core/test_responses.py -q`
Expected: PASS.

- [ ] **Step 9: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/_recovery.py src/kdive/mcp/responses.py \
        tests/mcp/lifecycle/test_recovery_helpers.py tests/mcp/core/test_responses.py
git commit -m "feat(lifecycle): recovery summary helpers + ToolResponse.failure refs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Allocation envelope recovery fields

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/allocations/common.py` (`envelope_for_allocation`)
- Test: `tests/mcp/lifecycle/test_allocations_tools.py` (extend)

**Interfaces:**
- Consumes: `iso` from `kdive.mcp.tools.lifecycle._recovery` (Task 1).
- Produces: `envelope_for_allocation` `data` now carries `requested_kind`, `requested_resource_id`, `requested_pcie_specs`, `shape`, `requested_vcpus`, `requested_memory_gb`, `requested_disk_gb`, `resource_id`, `lease_expiry`, `active_started_at`, `active_ended_at`, `created_at`, `updated_at` — on both the success and the failed envelope. Existing keys (`project`, queue counters) unchanged.

- [ ] **Step 1: Write the failing envelope test**

Append to `tests/mcp/lifecycle/test_allocations_tools.py` (reuse `Allocation`, `AllocationState`, `ResourceKind`, `_DT`, `_envelope_for_allocation`, `uuid4` already imported there):

```python
def test_envelope_surfaces_recovery_context_on_granted() -> None:
    res = uuid4()
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=res,
        state=AllocationState.GRANTED,
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
        requested_vcpus=4,
        requested_memory_gb=8,
        requested_disk_gb=40,
        shape="small",
    )
    data = _envelope_for_allocation(alloc).data
    assert data["resource_id"] == str(res)
    assert data["requested_kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert data["requested_vcpus"] == 4
    assert data["requested_memory_gb"] == 8
    assert data["requested_disk_gb"] == 40
    assert data["shape"] == "small"
    assert data["created_at"] == _DT.isoformat()
    assert data["requested_pcie_specs"] == []
    assert data["lease_expiry"] is None


def test_envelope_surfaces_selector_on_failed() -> None:
    alloc = Allocation(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="user-1",
        project="proj",
        resource_id=None,
        state=AllocationState.FAILED,
        requested_kind=ResourceKind.LOCAL_LIBVIRT,
        failure_category=ErrorCategory.ALLOCATION_DENIED,
    )
    resp = _envelope_for_allocation(alloc)
    assert resp.status == "error"
    assert resp.data["requested_kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert resp.data["resource_id"] is None
```

(If `ErrorCategory` is not yet imported in the test module, add
`from kdive.domain.errors import ErrorCategory`.)

- [ ] **Step 2: Run — expect failure**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_allocations_tools.py::test_envelope_surfaces_recovery_context_on_granted" -q`
Expected: FAIL — `KeyError: 'resource_id'`.

- [ ] **Step 3: Implement the recovery data in `envelope_for_allocation`**

In `src/kdive/mcp/tools/lifecycle/allocations/common.py`, add the import and a helper, and
merge the helper into both branches:

```python
from kdive.mcp.tools.lifecycle._recovery import iso


def _allocation_recovery(alloc: Allocation) -> dict[str, JsonValue]:
    """Selector, sizing, placement, and timing already on the Allocation row (#568)."""
    return {
        "requested_kind": alloc.requested_kind.value if alloc.requested_kind else None,
        "requested_resource_id": (
            str(alloc.requested_resource_id) if alloc.requested_resource_id else None
        ),
        "requested_pcie_specs": list(alloc.requested_pcie_specs),
        "shape": alloc.shape,
        "requested_vcpus": alloc.requested_vcpus,
        "requested_memory_gb": alloc.requested_memory_gb,
        "requested_disk_gb": alloc.requested_disk_gb,
        "resource_id": str(alloc.resource_id) if alloc.resource_id else None,
        "lease_expiry": iso(alloc.lease_expiry),
        "active_started_at": iso(alloc.active_started_at),
        "active_ended_at": iso(alloc.active_ended_at),
        "created_at": iso(alloc.created_at),
        "updated_at": iso(alloc.updated_at),
    }
```

Then in `envelope_for_allocation`, merge it into the failed branch and the success `data`:

```python
    recovery = _allocation_recovery(alloc)
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value, **recovery},
        )
    data: dict[str, JsonValue] = {"project": alloc.project, **recovery}
    if alloc.state is AllocationState.REQUESTED and queue_position is not None:
        data["queue_position"] = queue_position
        data["queue_ahead"] = queue_position - 1
```

(`iso(alloc.created_at)` returns `str` since `created_at` is non-null; the `str | None`
return type is fine for the `JsonValue` dict.)

- [ ] **Step 4: Run the allocation envelope + handler tests — expect pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/allocations/common.py tests/mcp/lifecycle/test_allocations_tools.py
git commit -m "feat(allocations): surface recovery context on get/wait/list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: System envelope recovery fields

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/view.py` (`system_envelope`, `get_system`, `list_systems`, `_split_kind`)
- Test: `tests/mcp/lifecycle/test_systems_tools.py` and/or `test_systems_list.py` (extend)

**Interfaces:**
- Consumes: `provisioning_profile_summary`, `iso` from `_recovery` (Task 1); `RunState` from `kdive.domain.capacity.state`.
- Produces: `system_envelope(system, *, resource_kind=None, resource_id=None, active_debug_session_ids=None, active_run=None)`. `data` carries `project` (existing), `allocation_id`, `resource_id`, `resource_kind`, the provisioning summary (`arch`/`boot_method`/`vcpu`/`memory_mb`/`disk_gb`), `shape`, `created_at`, `updated_at`, and (get-only) `active_run` + `active_debug_session_ids`.
- Produces helpers `_placement_for_system(conn, allocation_id) -> tuple[str | None, str | None]` and `_active_run_for_system(conn, system_id) -> dict[str, JsonValue] | None`.

- [ ] **Step 1: Write the failing envelope test**

Append to `tests/mcp/lifecycle/test_systems_tools.py` (import `System`, `SystemState`, `system_envelope`, `uuid4`, a datetime; follow the module's existing imports):

```python
def test_system_envelope_surfaces_placement_and_profile_summary() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from kdive.domain.capacity.state import SystemState
    from kdive.domain.lifecycle import System
    from kdive.mcp.tools.lifecycle.systems.view import system_envelope

    dt = datetime(2026, 6, 18, tzinfo=UTC)
    alloc_id, res_id, run_id = uuid4(), uuid4(), uuid4()
    system = System(
        id=uuid4(),
        created_at=dt,
        updated_at=dt,
        principal="user-1",
        project="proj",
        allocation_id=alloc_id,
        state=SystemState.READY,
        provisioning_profile={
            "schema_version": 1,
            "arch": "x86_64",
            "boot_method": "direct-kernel",
            "vcpu": 4,
            "memory_mb": 8192,
            "disk_gb": 40,
            "kernel_source_ref": "secret-tree",
        },
        shape="small",
    )
    resp = system_envelope(
        system,
        resource_kind="local-libvirt",
        resource_id=str(res_id),
        active_run={"id": str(run_id), "state": "running"},
    )
    data = resp.data
    assert data["allocation_id"] == str(alloc_id)
    assert data["resource_id"] == str(res_id)
    assert data["resource_kind"] == "local-libvirt"
    assert data["arch"] == "x86_64"
    assert data["boot_method"] == "direct-kernel"
    assert data["memory_mb"] == 8192
    assert data["shape"] == "small"
    assert data["created_at"] == dt.isoformat()
    assert data["active_run"] == {"id": str(run_id), "state": "running"}
    assert "secret-tree" not in str(data)
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_systems_tools.py::test_system_envelope_surfaces_placement_and_profile_summary" -q`
Expected: FAIL — `TypeError: system_envelope() got an unexpected keyword argument 'resource_id'`.

- [ ] **Step 3: Extend `system_envelope`**

In `src/kdive/mcp/tools/lifecycle/systems/view.py`, add imports near the top:

```python
from kdive.domain.capacity.state import RunState, SystemState  # RunState added
from kdive.mcp.tools.lifecycle._recovery import iso, provisioning_profile_summary
```

Replace `system_envelope` with:

```python
def system_envelope(
    system: System,
    *,
    resource_kind: str | None = None,
    resource_id: str | None = None,
    active_debug_session_ids: list[str] | None = None,
    active_run: dict[str, JsonValue] | None = None,
) -> ToolResponse:
    """Render a System with recovery context; ``failed`` becomes a failure envelope.

    ``resource_kind``/``resource_id`` are the backing Resource and the granted resource id
    (ADR-0169/0180). The provisioning summary, ``allocation_id``, ``shape``, and timestamps
    come from the System row (no extra query, both paths). ``active_run`` and
    ``active_debug_session_ids`` are get-only (an N+1 on the list path), omitted otherwise.
    """
    data: dict[str, JsonValue] = {
        "project": system.project,
        "allocation_id": str(system.allocation_id),
        "shape": system.shape,
        "created_at": iso(system.created_at),
        "updated_at": iso(system.updated_at),
        **provisioning_profile_summary(system.provisioning_profile),
    }
    if resource_kind is not None:
        data["resource_kind"] = resource_kind
    if resource_id is not None:
        data["resource_id"] = resource_id
    if active_debug_session_ids is not None:
        data["active_debug_session_ids"] = list(active_debug_session_ids)
    if active_run is not None:
        data["active_run"] = active_run
    if system.state is SystemState.FAILED:
        return ToolResponse.failure(
            str(system.id),
            ErrorCategory.INFRASTRUCTURE_FAILURE,
            data={"current_status": system.state.value, **data},
        )
    return ToolResponse.success(
        str(system.id),
        system.state.value,
        suggested_next_actions=["systems.get", "systems.teardown"],
        data=data,
    )
```

- [ ] **Step 4: Run the envelope test — expect pass**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_systems_tools.py::test_system_envelope_surfaces_placement_and_profile_summary" -q`
Expected: PASS.

- [ ] **Step 5: Add the get-path placement + active-run lookups**

In `src/kdive/mcp/tools/lifecycle/systems/view.py`, add two module-level helpers:

```python
async def _placement_for_system(
    conn: AsyncConnection, allocation_id: UUID
) -> tuple[str | None, str | None]:
    """Return ``(resource_id, resource_kind)`` for a System's allocation (one query)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT a.resource_id, r.kind FROM allocations a "
            "LEFT JOIN resources r ON r.id = a.resource_id WHERE a.id = %s",
            (allocation_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    resource_id, kind = row
    return (str(resource_id) if resource_id is not None else None), kind


async def _active_run_for_system(
    conn: AsyncConnection, system_id: UUID
) -> dict[str, JsonValue] | None:
    """The most-recent non-terminal run holding the System, or ``None`` (#568)."""
    terminal = [RunState.FAILED.value, RunState.CANCELED.value]
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM runs WHERE system_id = %s AND state <> ALL(%s) "
            "ORDER BY created_at DESC, id LIMIT 1",
            (system_id, terminal),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "state": row[1]}
```

Add the needed imports if absent: `from uuid import UUID`, `from psycopg import AsyncConnection`.

Then update `get_system` to call them:

```python
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            resource_id, resource_kind = await _placement_for_system(conn, system.allocation_id)
            active_sessions = await active_session_ids_for_system(conn, system.id)
            active_run = await _active_run_for_system(conn, system.id)
        return system_envelope(
            system,
            resource_kind=resource_kind,
            resource_id=resource_id,
            active_debug_session_ids=active_sessions,
            active_run=active_run,
        )
```

- [ ] **Step 6: Add `resource_id` to the list join**

In `list_systems`, add `a.resource_id AS resource_id` to the SELECT and update `_split_kind`
to pop it. Replace the query line:

```python
        query = sql.SQL(
            "SELECT s.*, r.kind AS resource_kind, a.resource_id AS resource_id FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "WHERE {where} ORDER BY s.created_at DESC, s.id LIMIT %s"
        ).format(where=sql.SQL(" AND ").join(filters.clauses))
```

Replace `_split_kind` and the collection builder:

```python
def _split_placement(row: dict[str, object]) -> tuple[System, str, str | None]:
    """Separate the joined resource kind + id from the System columns before validation."""
    resource_kind = str(row.pop("resource_kind"))
    resource_id = row.pop("resource_id")
    resource_id_str = str(resource_id) if resource_id is not None else None
    return System.model_validate(row), resource_kind, resource_id_str


def _systems_collection(systems: list[tuple[System, str, str | None]]) -> ToolResponse:
    """Render Systems (each with its backing Resource kind + id) into one envelope."""
    return ToolResponse.collection(
        "systems",
        "ok",
        [
            system_envelope(system, resource_kind=resource_kind, resource_id=resource_id)
            for system, resource_kind, resource_id in systems
        ],
        suggested_next_actions=["systems.get", "runs.create"],
    )
```

Update the two `list_systems` return sites to use `_split_placement`:
`return _systems_collection([_split_placement(row) for row in rows])` and the empty case
`return _systems_collection([])` (unchanged).

- [ ] **Step 7: Write/extend a list handler test asserting `resource_id` on list rows**

Add to `tests/mcp/lifecycle/test_systems_list.py` (follow that module's seed helpers/fixtures):
a test that seeds a ready System, calls `list_systems`, and asserts the first item's
`data["resource_id"]` equals the seeded resource id and `data["allocation_id"]` is present,
and that no `active_run`/`active_debug_session_ids` key appears on a list item.

```python
def test_list_systems_surfaces_placement(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # Seed an allocation+system using the module's existing helpers, capturing
            # the resource id and system's project, then:
            resp = await list_systems(pool, _ctx())
        item = resp.items[0]
        assert item.data["resource_id"] is not None
        assert "allocation_id" in item.data
        assert "active_run" not in item.data
    asyncio.run(_run())
```

(Wire the seeding to the helpers already used by `test_systems_list.py` — do not invent a
new fixture. If that module lacks a list seed helper, reuse the one in
`test_systems_tools.py`.)

- [ ] **Step 8: Run the system tests — expect pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py tests/mcp/lifecycle/test_systems_list.py -q`
Expected: PASS (new + existing).

- [ ] **Step 9: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/systems/view.py tests/mcp/lifecycle/test_systems_tools.py tests/mcp/lifecycle/test_systems_list.py
git commit -m "feat(systems): surface placement, profile summary, active run on get/list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Run envelope recovery fields

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py` (`envelope_for_run`, `_failed_envelope`)
- Test: `tests/mcp/lifecycle/test_runs_tools.py` (extend)

**Interfaces:**
- Consumes: `build_profile_summary` from `_recovery` (Task 1); `ToolResponse.failure(refs=...)` (Task 1).
- Produces: `envelope_for_run` `data` gains `investigation_id`, `build_source`, `build_host` (when present), `build_source_provenance`; the envelope `refs` gains `kernel`/`debuginfo` when set — on both the success and the failed path.

- [ ] **Step 1: Write the failing envelope tests**

Append to `tests/mcp/lifecycle/test_runs_tools.py` (reuse the module's `Run`, `RunState`,
`ResourceKind`, `envelope_for_run`, `uuid4`, datetime helpers; add imports if missing):

```python
def test_run_envelope_surfaces_investigation_build_and_artifacts() -> None:
    inv_id = uuid4()
    run = _make_run(  # use the module's run factory; otherwise construct Run(...) directly
        state=RunState.SUCCEEDED,
        investigation_id=inv_id,
        build_profile={
            "source": "server",
            "build_host": "build-1",
            "kernel_source_ref": {"git": {"remote": "https://h/r", "ref": "main"}},
        },
        kernel_ref="s3://bucket/vmlinuz",
        debuginfo_ref="s3://bucket/vmlinux",
    )
    resp = envelope_for_run(run)
    assert resp.data["investigation_id"] == str(inv_id)
    assert resp.data["build_source"] == "server"
    assert resp.data["build_host"] == "build-1"
    assert resp.data["build_source_provenance"] == "git"
    assert resp.refs == {"kernel": "s3://bucket/vmlinuz", "debuginfo": "s3://bucket/vmlinux"}
    assert "h/r" not in str(resp.data)


def test_failed_run_envelope_keeps_investigation_and_artifacts() -> None:
    run = _make_run(
        state=RunState.FAILED,
        failure_category=ErrorCategory.INSTALL_FAILURE,
        kernel_ref="s3://bucket/vmlinuz",
    )
    resp = envelope_for_run(run)
    assert resp.status == "error"
    assert "investigation_id" in resp.data
    assert resp.refs == {"kernel": "s3://bucket/vmlinuz"}
```

If the module has no `_make_run` factory, construct directly, e.g.:

```python
    run = Run(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project="proj",
        investigation_id=inv_id, system_id=None, target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.SUCCEEDED, build_profile={...},
        kernel_ref="s3://bucket/vmlinuz", debuginfo_ref="s3://bucket/vmlinux",
    )
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run python -m pytest "tests/mcp/lifecycle/test_runs_tools.py::test_run_envelope_surfaces_investigation_build_and_artifacts" -q`
Expected: FAIL — `KeyError: 'investigation_id'`.

- [ ] **Step 3: Implement the run recovery data + refs**

In `src/kdive/mcp/tools/lifecycle/runs/common.py`, add the import and two small helpers:

```python
from kdive.mcp.tools.lifecycle._recovery import build_profile_summary


def _run_recovery(run: Run) -> dict[str, JsonValue]:
    """Investigation link + redaction-safe build summary, on the Run row (#568)."""
    return {"investigation_id": str(run.investigation_id), **build_profile_summary(run.build_profile)}


def _run_artifact_refs(run: Run) -> dict[str, str]:
    """The Run's object-store artifact keys, for the envelope ``refs`` slot."""
    refs: dict[str, str] = {}
    if run.kernel_ref:
        refs["kernel"] = run.kernel_ref
    if run.debuginfo_ref:
        refs["debuginfo"] = run.debuginfo_ref
    return refs
```

In `envelope_for_run`, merge `_run_recovery(run)` into `data` and pass refs to
`ToolResponse.success` (after the existing `data` is assembled, before the return):

```python
    data.update(_run_recovery(run))
    ...
    return ToolResponse.success(
        str(run.id),
        run.state.value,
        suggested_next_actions=actions,
        refs=_run_artifact_refs(run),
        data=data,
    )
```

In `_failed_envelope`, merge the recovery data and pass refs:

```python
    data: dict[str, JsonValue] = {"current_status": run.state.value, **_run_recovery(run)}
    ...
    return ToolResponse.failure(
        str(run.id), category, detail=detail, refs=_run_artifact_refs(run), data=data
    )
```

(The investigation link and build summary carry no resource-existence signal — the Run is
already project-scoped — and the artifact refs are the Run's own keys, so they are added
unconditionally, not gated by the no-leak `suppressed_detail` seam.)

- [ ] **Step 4: Run the run tests — expect pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
Expected: PASS (new + existing).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/runs/common.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(runs): surface investigation, build summary, artifact refs on get

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Redaction guard, resume integration, and full-suite verification

**Files:**
- Test: `tests/mcp/lifecycle/test_recovery_redaction.py` (create), `tests/integration/` resume test (create or extend an existing lifecycle integration module)
- Verify: generated docs (`just docs-check`), full `just ci`.

**Interfaces:**
- Consumes: all three envelope functions and the `_recovery` helpers.

- [ ] **Step 1: Write the redaction guard test**

Create `tests/mcp/lifecycle/test_recovery_redaction.py`. Build a System whose
`provisioning_profile` carries a credential-bearing `ssh_credential_ref` and a Run whose
`build_profile` carries a git remote with an inline token; render both envelopes; assert the
secret substrings are absent from `model_dump_json()`:

```python
"""No free-form profile reference string leaks into a recovery envelope (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle import Run, System
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.mcp.tools.lifecycle.systems.view import system_envelope

_PLANTED = "PLANTED-DO-NOT-LEAK"  # a benign marker; the test asserts it never leaks
_DT = datetime(2026, 6, 18, tzinfo=UTC)


def test_system_envelope_excludes_ssh_credential_ref() -> None:
    system = System(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="u", project="proj",
        allocation_id=uuid4(), state=SystemState.READY,
        provisioning_profile={
            "schema_version": 1, "arch": "x86_64", "boot_method": "direct-kernel",
            "vcpu": 2, "memory_mb": 4096, "disk_gb": 20,
            "provider": {"local-libvirt": {"ssh_credential_ref": f"file:///run/{_PLANTED}"}},
        },
    )
    resp = system_envelope(system, resource_kind="local-libvirt", resource_id=str(uuid4()))
    assert _PLANTED not in resp.model_dump_json()


def test_run_envelope_excludes_git_remote_token() -> None:
    run = Run(
        id=uuid4(), created_at=_DT, updated_at=_DT, principal="u", project="proj",
        investigation_id=uuid4(), system_id=None, target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.SUCCEEDED,
        build_profile={
            "source": "server", "build_host": "build-1",
            # Secret embedded as a path segment (not basic-auth userinfo, which the
            # detect-secrets hook would flag); the test asserts it does not leak.
            "kernel_source_ref": {"git": {"remote": f"https://h/{_PLANTED}/r.git", "ref": "main"}},
        },
    )
    resp = envelope_for_run(run)
    assert _PLANTED not in resp.model_dump_json()
```

- [ ] **Step 2: Run the redaction guard — expect pass (the implementation already excludes refs)**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_recovery_redaction.py -q`
Expected: PASS. If it FAILS, a free-form ref leaked — fix the offending summary helper, do
not weaken the test.

- [ ] **Step 3: Write the resume integration test**

Find the existing lifecycle integration exercise (search `tests/integration/` for a module
that drives allocation→system→run over the handlers with the `migrated_url`/stack fixtures).
Add a test that: requests+grants an allocation, provisions a System, creates+binds a Run,
then calls `get_allocation`, `get_system`, `get_run` and asserts that the ids needed for the
next tool are all present in the envelopes without touching the DB directly:

```python
def test_resume_from_read_tools(migrated_url: str) -> None:
    async def _run() -> None:
        # ... seed via the module's existing helpers: alloc(granted) -> system(ready)
        #     -> run(created, bound to system) ...
        alloc_resp = await get_allocation(pool, ctx, alloc_id)
        sys_resp = await get_system(pool, ctx, system_id)
        run_resp = await get_run(pool, ctx, run_id, resolver=resolver)
        # systems.provision needs the granted resource id:
        assert alloc_resp.data["resource_id"] is not None
        # runs.bind / install need the system + its allocation + run's investigation:
        assert sys_resp.data["allocation_id"] is not None
        assert sys_resp.data["resource_kind"] is not None
        assert run_resp.data["system_id"] == system_id
        assert run_resp.data["investigation_id"] is not None
    asyncio.run(_run())
```

(Wire seeding to the module's existing helpers/fixtures; do not invent new infrastructure.
If no suitable integration module exists, place this in `tests/mcp/lifecycle/` using the
same `migrated_url` handler-seeding pattern the unit handler tests use.)

- [ ] **Step 4: Run the resume test — expect pass**

Run: `uv run python -m pytest -k resume_from_read_tools -q`
Expected: PASS.

- [ ] **Step 5: Regenerate generated docs and run the full gate**

The new `data`/`refs` keys do not change tool input/output schemas (ADR-0113), so the
generated tool reference should be unchanged — but verify, and regenerate if the generator
picked up anything:

```bash
just docs-check || just docs
just ci
```

Expected: `just ci` PASS end-to-end (lint, type, lint-shell, lint-workflows, check-mermaid,
docs-links, docs-paths, adr-status-check, docs-check, config-docs-check, config-guard,
env-docs-check, resources-docs-check, chart-version-check, test).

- [ ] **Step 6: Commit**

```bash
git add tests/
# include docs/ only if `just docs` actually changed a generated file
git commit -m "test(lifecycle): redaction guard + resume-from-read-tools coverage

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review notes

- **Spec coverage:** allocation fields → Task 2; system fields (allocation_id/resource_id/
  resource_kind/profile summary/shape/timestamps/active_run) → Task 3; run fields
  (investigation_id/build summary/artifact refs, success + failure) → Task 4; redaction
  allowlist + no-raw-profile → Task 1 helpers, guarded in Task 5; no-N+1 → Task 3 keeps list
  single-query (placement from the existing join; active_run/sessions get-only); resume test
  → Task 5.
- **Type consistency:** `system_envelope` kwargs (`resource_kind`, `resource_id`,
  `active_debug_session_ids`, `active_run`) match Task 3's call sites in `get_system`/
  `_systems_collection`. `ToolResponse.failure(refs=...)` defined in Task 1 is used in Tasks
  3 and 4. `_recovery` helper names (`iso`, `build_profile_summary`,
  `provisioning_profile_summary`) are identical across Tasks 1–4.
- **Memory units:** allocation surfaces `requested_memory_gb` (GB); system surfaces
  `memory_mb` (MB) — intentional, mirrors the source columns (documented in the spec).
