# Expected-crash capture disclosure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `expected_crash_observed` boot outcome record and surface, on `runs.get`, which capture methods are reachable (`available_capture=["console"]`) and which provisioned flags are inert (`inert_capture`), so an agent learns at boot that its `preserve_on_crash`/`gdbstub`/`crashkernel` flags will not fire on a declared early-boot crash.

**Architecture:** Record `available_capture` + `inert_capture` in the boot step result at boot time (the only point where Run, System, and profile coincide); read them generically in `StepProgress`; surface them as `data.available_capture` / `data.inert_capture` on the `runs.get` envelope. No re-routing of the outcome (the console A/B flow per ADR-0227/#759 stays); no migration, tool, schema, or authz change. See spec `docs/superpowers/specs/2026-06-24-expected-crash-capture-disclosure-design.md` and ADR-0239.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`. Guardrails: `just lint`, `just type`, `just test`.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; Google-style docstrings on non-trivial public APIs.
- `ty` is whole-tree (`src` + `tests`); run `just type`, not a scoped check. (Local-only divergence: whole-tree `ty` may fail on unrelated `drgn`/`libguestfs` unused-ignore noise; if and only if that is the sole failure and CI is green, `SKIP=ty` is acceptable locally — never to mask a real error.)
- `available_capture` for `expected_crash_observed` is **always** `["console"]` (the reachable-now set on the `READY`-staying System).
- `inert_capture` order is deterministic: `gdbstub`, `host_dump`, `kdump`.
- Use `CaptureMethod.*.value` strings, never literals, for the method tokens.
- Compute the inert set from the provider-neutral `ProfilePolicy` predicates only (`gdbstub_provisioned`, `host_dump_provisioned`, `capture_method`), never a provider-specific profile section.
- Per-commit: run `just lint`, `just type`, and the focused test module green before committing. Conventional-commit subject ≤72 chars; end every commit with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Do NOT change `_succeeded_next_step` or `sessions_lifecycle.py` — those surfaces are #759's territory.

---

### Task 1: Boot-result disclosure for the expected-crash outcome

**Files:**
- Modify: `src/kdive/jobs/handlers/runs_boot.py` (add `_inert_capture`; refactor the `expected_crash_observed` branch of `_run_boot_and_capture_outcome` into `_record_expected_crash`)
- Test: `tests/jobs/handlers/test_runs_boot.py`

**Interfaces:**
- Consumes: `ProfilePolicy` (`gdbstub_provisioned`, `host_dump_provisioned`, `capture_method`), `CaptureMethod`, `SYSTEMS.get`, `ProvisioningProfile.parse`, the existing `_record_boot_audit`, `_capture_run_console`.
- Produces: the `expected_crash_observed` boot-step result dict now also carries `"available_capture": ["console"]` and `"inert_capture": [...]`. Later tasks read these keys from the persisted `run_steps(step='boot').result`.

- [ ] **Step 1: Extend the `_Pol` test fake with `capture_method`**

`_inert_capture` calls `profile_policy.capture_method(profile)`, which the existing `_Pol` fake (`tests/jobs/handlers/test_runs_boot.py:197-207`) does not implement. Add it, defaulting to a non-KDUMP method, with an optional override:

```python
class _Pol:
    """Fake ProfilePolicy carrying just the predicates the recording path reads."""

    def __init__(self, *, gdbstub: bool, host_dump: bool, kdump: bool = False) -> None:
        self._gdbstub, self._host_dump, self._kdump = gdbstub, host_dump, kdump

    def gdbstub_provisioned(self, _profile: object) -> bool:
        return self._gdbstub

    def host_dump_provisioned(self, _profile: object) -> bool:
        return self._host_dump

    def capture_method(self, _profile: object) -> CaptureMethod:
        return CaptureMethod.KDUMP if self._kdump else CaptureMethod.CONSOLE
```

Add `from kdive.domain.capture import CaptureMethod` to the test imports and update `_pol(...)` and the `_record(...)` helper's `_Pol(...)` construction to pass `kdump=False` by default (they already pass `gdbstub`/`host_dump` by keyword, so adding the default keyword needs no call-site change).

- [ ] **Step 2: Write the failing `_inert_capture` tests**

```python
def test_inert_capture_empty_for_console_only_profile() -> None:
    out = runs_boot._inert_capture(
        _pol(gdbstub=False, host_dump=False), cast(ProvisioningProfile, object())
    )
    assert out == []


def test_inert_capture_orders_gdbstub_host_dump_kdump() -> None:
    pol = cast(ProfilePolicy, _Pol(gdbstub=True, host_dump=True, kdump=True))
    out = runs_boot._inert_capture(pol, cast(ProvisioningProfile, object()))
    assert out == ["gdbstub", "host_dump", "kdump"]


def test_inert_capture_kdump_only_when_crashkernel_set() -> None:
    pol = cast(ProfilePolicy, _Pol(gdbstub=False, host_dump=False, kdump=True))
    out = runs_boot._inert_capture(pol, cast(ProvisioningProfile, object()))
    assert out == ["kdump"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k inert_capture -q`
Expected: FAIL with `AttributeError: module 'kdive.jobs.handlers.runs_boot' has no attribute '_inert_capture'`.

- [ ] **Step 4: Implement `_inert_capture`**

Add beside `_available_capture` in `src/kdive/jobs/handlers/runs_boot.py`:

```python
def _inert_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    """Capture methods the System was provisioned for that will NOT fire on an expected crash.

    The ``expected_crash_observed`` outcome leaves the System ``READY`` and is routed to the
    console A/B flow (ADR-0227), so a provisioned ``gdbstub`` (live-attach refused), ``host_dump``
    / ``kdump`` (both need ``CRASHED``) are inert here. Built from provider-neutral
    ``ProfilePolicy`` predicates so the generic boot handler stays correct for every provider
    (ADR-0239).
    """
    methods: list[str] = []
    if profile_policy.gdbstub_provisioned(profile):
        methods.append(CaptureMethod.GDBSTUB.value)
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    if profile_policy.capture_method(profile) is CaptureMethod.KDUMP:
        methods.append(CaptureMethod.KDUMP.value)
    return methods
```

- [ ] **Step 5: Run the `_inert_capture` tests to verify they pass**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k inert_capture -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Write the failing `_record_expected_crash` tests**

Add a recorder helper + tests mirroring `_record(...)`. The expected branch needs only the System fetch, console capture, audit, and policy — no connector/gdbstub probe:

```python
def _record_expected(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gdbstub: bool,
    host_dump: bool,
    kdump: bool,
    system_present: bool,
) -> tuple[dict[str, object] | None, list[object]]:
    audits: list[object] = []

    async def _fake_get(_conn: object, _system_id: object) -> _FakeSystem | None:
        return _FakeSystem(_PROFILE_DICT) if system_present else None

    async def _fake_audit(_conn: object, _ctx: object, run: object) -> None:
        audits.append(run)

    monkeypatch.setattr(runs_boot.SYSTEMS, "get", _fake_get)
    monkeypatch.setattr(runs_boot, "_record_boot_audit", _fake_audit)

    artifact = runs_boot._ConsoleArtifact(uuid4(), "tenant/console", _PANIC_CONSOLE)

    async def _run() -> dict[str, object] | None:
        return await runs_boot._record_expected_crash(
            cast(AsyncConnection, object()),
            cast(RequestContext, object()),
            cast(Run, _FakeRun({"kind": "console_crash", "pattern": "panic"})),
            uuid4(),
            cast(ProfilePolicy, _Pol(gdbstub=gdbstub, host_dump=host_dump, kdump=kdump)),
            artifact,
        )

    return asyncio.run(_run()), audits


def test_record_expected_crash_discloses_console_and_inert() -> None:
    result, audits = _record_expected(
        monkeypatch=pytest.MonkeyPatch(),
        gdbstub=True,
        host_dump=True,
        kdump=False,
        system_present=True,
    )
    assert result is not None
    assert result["boot_outcome"] == "expected_crash_observed"
    assert result["expectation_matched"] is True
    assert result["available_capture"] == ["console"]
    assert result["inert_capture"] == ["gdbstub", "host_dump"]
    assert len(audits) == 1


def test_record_expected_crash_degrades_when_system_gone() -> None:
    result, _ = _record_expected(
        monkeypatch=pytest.MonkeyPatch(),
        gdbstub=True,
        host_dump=True,
        kdump=True,
        system_present=False,
    )
    assert result is not None
    assert result["available_capture"] == ["console"]
    assert result["inert_capture"] == []
```

Note: the two functions above construct their own `pytest.MonkeyPatch()` so `_record_expected` needs no `monkeypatch` fixture threading; if the repo's lint flags an unused fixture, use the fixture-injected `monkeypatch` instead by giving each test a `monkeypatch: pytest.MonkeyPatch` parameter and passing it through.

- [ ] **Step 7: Run the `_record_expected_crash` tests to verify they fail**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -k record_expected_crash -q`
Expected: FAIL with `AttributeError: ... has no attribute '_record_expected_crash'`.

- [ ] **Step 8: Implement `_record_expected_crash` and call it from the boot outcome**

Add the helper (place it just above `_run_boot_and_capture_outcome`):

```python
async def _record_expected_crash(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    system_id: UUID,
    profile_policy: ProfilePolicy,
    artifact: _ConsoleArtifact,
) -> dict[str, Any]:
    """Record ``expected_crash_observed``, disclosing the reachable + inert capture surface.

    The System stays ``READY`` and is routed to the console A/B flow (ADR-0227), so
    ``available_capture`` is ``["console"]``; ``inert_capture`` lists the provisioned-but-unreachable
    methods (ADR-0239). A missing System row degrades to an empty inert set rather than failing
    the outcome.
    """
    system = await SYSTEMS.get(conn, system_id)
    inert: list[str] = []
    if system is not None:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
        inert = _inert_capture(profile_policy, profile)
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "expected_crash_observed",
        "expectation_matched": True,
        "evidence_kind": "console",
        "evidence_artifact_id": str(artifact.id),
        "available_capture": [CaptureMethod.CONSOLE.value],
        "inert_capture": inert,
    }
```

Then replace the inline expected-crash block in `_run_boot_and_capture_outcome` (currently the `if artifact is not None and artifact.data and _expected_crash_matches(...)` arm that returns the dict) so it delegates:

```python
        if artifact is not None and artifact.data and _expected_crash_matches(run, artifact.data):
            return await _record_expected_crash(
                conn, job_ctx, run, system_id, profile_policy, artifact
            )
```

`_run_boot_and_capture_outcome` already receives `profile_policy`; no signature change.

- [ ] **Step 9: Run the focused module to verify pass + no regression**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q`
Expected: PASS (all, including the unchanged `crashed_halted_live` / `_available_capture` tests).

- [ ] **Step 10: Lint + type, then commit**

Run: `just lint && just type`
Expected: clean (see Global Constraints on the local `ty` divergence).

```bash
git add src/kdive/jobs/handlers/runs_boot.py tests/jobs/handlers/test_runs_boot.py
git commit -m "feat(runs): disclose capture surface on expected-crash boot outcome

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Read `available_capture` / `inert_capture` into `StepProgress`

**Files:**
- Modify: `src/kdive/services/runs/steps.py` (add `_optional_str_list`; two `StepProgress` fields; read them in `step_progress`)
- Test: `tests/services/runs/test_steps.py` (pure coercion unit tests), `tests/mcp/lifecycle/test_runs_tools.py` (DB-backed `step_progress` surfacing)

**Interfaces:**
- Consumes: the boot-step result keys `available_capture` / `inert_capture` written in Task 1.
- Produces: `StepProgress.available_capture: list[str] | None` and `StepProgress.inert_capture: list[str] | None`. Task 3 reads these.

- [ ] **Step 1: Write the failing `_optional_str_list` unit tests**

Add to `tests/services/runs/test_steps.py` (import `from kdive.services.runs.steps import _optional_str_list`):

```python
def test_optional_str_list_passes_through_string_list() -> None:
    assert _optional_str_list(["console", "gdbstub"]) == ["console", "gdbstub"]


def test_optional_str_list_empty_list_is_empty_not_none() -> None:
    assert _optional_str_list([]) == []


def test_optional_str_list_rejects_non_list() -> None:
    assert _optional_str_list("console") is None


def test_optional_str_list_rejects_non_string_member() -> None:
    assert _optional_str_list(["console", 3]) is None
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `uv run python -m pytest tests/services/runs/test_steps.py -k optional_str_list -q`
Expected: FAIL with `ImportError: cannot import name '_optional_str_list'`.

- [ ] **Step 3: Implement `_optional_str_list` and extend `StepProgress` + `step_progress`**

In `src/kdive/services/runs/steps.py`, beside `_optional_str` (line 33):

```python
def _optional_str_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item for item in value if isinstance(item, str)]
```

Add two fields to `StepProgress` (after `console_evidence_artifact_id`, both defaulting to `None` so the existing exact-match `StepProgress(...)` assertion in `test_runs_tools.py` stays valid):

```python
    available_capture: list[str] | None = None
    inert_capture: list[str] | None = None
```

In `step_progress`, inside the `if row["step"] == "boot" and isinstance(row["result"], Mapping):` block, after the `console_evidence_artifact_id` read, add:

```python
            available_capture = _optional_str_list(boot_result.get("available_capture"))
            inert_capture = _optional_str_list(boot_result.get("inert_capture"))
```

Initialize `available_capture: list[str] | None = None` and `inert_capture: list[str] | None = None` beside the other locals, and pass them into the returned `StepProgress(...)`. Update the function docstring to mention the two new fields (one sentence; ADR-0239).

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run python -m pytest tests/services/runs/test_steps.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failing DB-backed surfacing test**

In `tests/mcp/lifecycle/test_runs_tools.py`, add a test beside the existing `step_progress` test (the one near the `Jsonb({"boot_outcome": "expected_crash_observed"})` insert, ~line 710). Model it on that test but write a richer boot result and assert the new fields:

```python
def test_step_progress_surfaces_capture_disclosure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'install', 'succeeded', %s)",
                    (UUID(run_id), Jsonb({})),
                )
                await conn.execute(
                    "INSERT INTO run_steps (run_id, step, state, result) "
                    "VALUES (%s, 'boot', 'succeeded', %s)",
                    (
                        UUID(run_id),
                        Jsonb(
                            {
                                "boot_outcome": "expected_crash_observed",
                                "available_capture": ["console"],
                                "inert_capture": ["gdbstub", "host_dump"],
                            }
                        ),
                    ),
                )
                progress = await step_progress(conn, UUID(run_id))
        assert progress.available_capture == ["console"]
        assert progress.inert_capture == ["gdbstub", "host_dump"]

    asyncio.run(_run())
```

- [ ] **Step 6: Run the DB-backed test to verify it fails, then passes**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k step_progress -q`
Expected: first FAIL (the assertion attributes do not yet flow — if Step 3 was committed before this, the test passes immediately; either way confirm green after Step 3). Then PASS for the whole `-k step_progress` selection (the pre-existing exact-match `StepProgress(...)` test stays green because the new fields default to `None`).

- [ ] **Step 7: Lint + type, then commit**

Run: `just lint && just type`

```bash
git add src/kdive/services/runs/steps.py tests/services/runs/test_steps.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(runs): read capture disclosure into StepProgress

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Surface `data.available_capture` / `data.inert_capture` on `runs.get`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py` (`envelope_for_run`, SUCCEEDED branch)
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: `StepProgress.available_capture` / `StepProgress.inert_capture` from Task 2 (via `step_progress` already threaded into `envelope_for_run`).
- Produces: `runs.get` response `data.available_capture` / `data.inert_capture` when the boot result carried them; absent otherwise.

- [ ] **Step 1: Write the failing envelope test**

In `tests/mcp/lifecycle/test_runs_tools.py`, beside `test_get_expected_crash_boot_recommends_triage` (~line 833):

```python
def test_get_expected_crash_surfaces_capture_disclosure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool,
                run_id,
                "boot",
                "succeeded",
                {
                    "boot_outcome": "expected_crash_observed",
                    "available_capture": ["console"],
                    "inert_capture": ["gdbstub", "host_dump"],
                },
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert resp.data["available_capture"] == ["console"]
        assert resp.data["inert_capture"] == ["gdbstub", "host_dump"]

    asyncio.run(_run())


def test_get_boot_without_disclosure_omits_capture_keys(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.SUCCEEDED)
            await _insert_step(pool, run_id, "install", "succeeded", {})
            await _insert_step(
                pool, run_id, "boot", "succeeded", {"boot_outcome": "expected_crash_observed"}
            )
            resp = await get_run(pool, _ctx(), run_id)
        assert "available_capture" not in resp.data
        assert "inert_capture" not in resp.data

    asyncio.run(_run())
```

- [ ] **Step 2: Run the envelope tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k "surfaces_capture_disclosure or omits_capture_keys" -q`
Expected: FAIL (`KeyError: 'available_capture'`).

- [ ] **Step 3: Surface the keys in `envelope_for_run`**

In `src/kdive/mcp/tools/lifecycle/runs/common.py`, in the SUCCEEDED `data` assembly (after the `steps` block, before the `return`), add:

```python
    if step_progress is not None and step_progress.available_capture is not None:
        data["available_capture"] = cast(JsonValue, step_progress.available_capture)
    if step_progress is not None and step_progress.inert_capture is not None:
        data["inert_capture"] = cast(JsonValue, step_progress.inert_capture)
```

(`step_progress` is the parameter name already in scope; `cast` and `JsonValue` are already imported.)

- [ ] **Step 4: Run the envelope tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -k "surfaces_capture_disclosure or omits_capture_keys" -q`
Expected: PASS.

- [ ] **Step 5: Lint + type, then commit**

Run: `just lint && just type`

```bash
git add src/kdive/mcp/tools/lifecycle/runs/common.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat(runs): surface capture disclosure on runs.get

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Pre-existing-assertion audit + full suite

**Files:** none expected (verification task; fold any fix into the task that owns the file).

- [ ] **Step 1: Grep for exact-match `data`/`StepProgress` assertions on the affected paths**

Run:
```bash
rg -n "data == \{|StepProgress\(|assert set\(.*data" tests/mcp/lifecycle/test_runs_tools.py tests/services/runs/test_steps.py
```
Confirm the only exact-match `StepProgress(...)` assertion (the `expected_crash_observed` `step_progress` test) still holds because the new fields default to `None`, and the `data == {...}` hit is the unrelated build-job envelope. If any assertion would break, update it in the file it lives in and re-commit under that file's task.

- [ ] **Step 2: Run the full suite**

Run: `just test`
Expected: all pass (`live_vm` is skipped by marker; this change needs no live host).

- [ ] **Step 3: Full guardrails**

Run: `just lint && just type && just check-mermaid && just docs-links`
Expected: clean.

---

## Self-Review

- **Spec coverage:** `available_capture=["console"]` + `inert_capture` recorded on `expected_crash_observed` → Task 1. Provider-neutral predicate computation → Task 1 (`_inert_capture`). Degraded (System gone) path → Task 1. Generic read of both keys (incl. `crashed_halted_live`'s `available_capture`) → Task 2. `runs.get` surfacing, keys omitted when absent → Task 3. Pre-existing exact-match assertion audit → Task 4. Out-of-scope items (next-action wording, gap-4, create-time advisory) are deliberately untouched.
- **Placeholder scan:** none — every code/step block is concrete.
- **Type consistency:** `_inert_capture(profile_policy, profile) -> list[str]`, `_record_expected_crash(...) -> dict[str, Any]`, `_optional_str_list(value) -> list[str] | None`, `StepProgress.available_capture/inert_capture: list[str] | None` are used identically across tasks. The boot-result keys `available_capture`/`inert_capture` and outcome string `"expected_crash_observed"` match across Tasks 1-3.
