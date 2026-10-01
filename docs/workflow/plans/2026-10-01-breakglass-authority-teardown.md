# Plan: `ops.force_teardown` authority routing (#3007)

Goal: break-glass `ops.force_teardown` routes a System with external-boot activation history
through `systems.teardown`'s authority teardown, and keeps the admission matrix call on its
ordinary path. Spec: [`2026-10-01-breakglass-authority-teardown-design.md`](../specs/2026-10-01-breakglass-authority-teardown-design.md).

Architecture: two public helpers extracted from `systems.teardown`'s locked body in
`src/kdive/mcp/tools/lifecycle/systems/admin.py`; break-glass calls them under its own System lock.
Tech stack: Python 3.14, psycopg async, pytest with disposable Postgres (Docker).

Expected implementation size: 120–190 changed lines (M) — two helper extractions (~40), break-glass
wiring and docstring (~25), three DB-backed tests (~90), ADR amendment (~20).

## Global Constraints

- Ruff line length 100; lint `E,F,I,UP,B,SIM`; `ty` strict, whole tree (`just type`).
- No new dependency, ADR, or migration. ADR-0620 is accepted: append a dated
  `### Amendment (2026-10-01): ... (#3007)` block only; never rewrite existing lines.
- Do not touch `src/kdive/jobs/service_operations.py` or
  `src/kdive/services/investigations/lifecycle.py` (part b, #3025).
- Prose: plain; no "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, `just records`
  (after `git fetch origin main`), pre-push `just ci > <file> 2>&1 < /dev/null`.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | extract `route_external_boot_teardown`, `ordinary_teardown_denial`; `_teardown_locked` calls them | the teardown routing policy for both tools |
| `src/kdive/mcp/tools/ops/security/breakglass.py` | `force_teardown(..., resolver=None)`; `_teardown_locked` routes; `register` passes `resolver`; wrapper docstring | break-glass auth, audit, short-circuits |
| `tests/mcp/lifecycle/test_systems_tools.py` | parametrize the #2966 replace test over both tools; add no-resolver and admission-denial tests | |
| `docs/adr/0620-authority-owned-system-teardown.md` | append amendment | |

## Task 1: Extract the routing helpers in `admin.py` (no behavior change)

Interfaces (produced, used by Task 2):

```python
async def route_external_boot_teardown(
    conn: AsyncConnection, ctx: RequestContext, system: System, system_id: str,
    idempotency_key: str | None, resolver: ProviderResolver | None,
) -> ToolResponse | None
async def ordinary_teardown_denial(
    conn: AsyncConnection, ctx: RequestContext, system: System, system_id: str,
) -> ToolResponse | None
```

Verification:
- Contract: `systems.teardown` behavior unchanged. Mode: focused-test — existing
  `tests/mcp/lifecycle/test_systems_tools.py -k teardown` stays green; red observation not
  applicable to a pure extraction, so green before and after is the evidence. Command:
  `just test-verbose tests/mcp/lifecycle/test_systems_tools.py` → all pass.

Steps:
1. Add after `_teardown_locked`:

```python
async def route_external_boot_teardown(conn, ctx, system, system_id, idempotency_key, resolver):
    """Route a System with any external-boot activation to its authority teardown, else None.

    The caller holds the System advisory lock; shared by `systems.teardown` and break-glass
    `ops.force_teardown` (#3007), whose ordinary job the worker would refuse (#2966).
    """
    activation = await _EXTERNAL_BOOT_ACTIVATIONS.get_latest_for_system(conn, system.id)
    if activation is None:
        return None
    return await _enqueue_authority_teardown(
        conn, ctx, system, activation, system_id, idempotency_key, resolver
    )


async def ordinary_teardown_denial(conn, ctx, system, system_id):
    """Run the admission matrix for an ordinary teardown; the typed denial, or None."""
    try:
        await check_external_boot_admission(
            conn, system.id, ExternalBootOperation.SYSTEM_TEARDOWN, project=system.project
        )
    except ExternalBootDenied as exc:
        return _external_boot_denial(system_id, exc, ctx)
    return None
```
   (with full annotations as in Interfaces).
2. In `_teardown_locked`, replace the activation read/branch with
   `routed = await route_external_boot_teardown(...)`; `if routed is not None: return routed`; and
   replace the admission `try/except` with `denial = await ordinary_teardown_denial(...)`;
   `if denial is not None: return denial`. Keep the comment above the admission call.
3. Run `just lint`, `just type`, the focused command. Commit
   `refactor(systems): extract teardown routing helpers for reuse`.

## Task 2: Route break-glass teardown

Files: `src/kdive/mcp/tools/ops/security/breakglass.py`, `tests/mcp/lifecycle/test_systems_tools.py`.
Consumes Task 1's helpers.

Verification:
- Contract S1/S2/S6: Mode: focused-test — parametrize
  `test_teardown_replaces_failed_ordinary_job_for_external_boot_history` with
  `via in ("systems.teardown", "ops.force_teardown")`; the break-glass arm calls
  `breakglass.force_teardown(pool, admin, system_id=..., reason="stuck",
  resolver=provider_resolver(external_boot=ExternalBootOperations()))` with `admin =
  RequestContext(principal="ops-admin", agent_session="s", projects=(), roles={},
  platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))` built inline, and asserts
  `SELECT count(*) FROM platform_audit_log WHERE tool = 'ops.force_teardown'` is 1. Import
  `from kdive.mcp.tools.ops.security import breakglass`. Red before Task 2: the break-glass arm fails with `TypeError`
  (no `resolver` kwarg), and with the kwarg ignored it returns the recycled ordinary job without
  `external_boot_authority_v1`.
- Contract S3: Mode: focused-test — `test_force_teardown_external_boot_history_needs_resolver`:
  same seed, `resolver=None` → `configuration_error`, `data.reason ==
  "external_boot_teardown_authority_unresolved"`, row still `failed`. Red before: returns
  `queued` (ordinary recycle).
- Contract S4: Mode: focused-test — `test_force_teardown_ordinary_path_runs_admission_matrix`:
  no activation; monkeypatch `admin.check_external_boot_admission` to raise
  `ExternalBootDenied("denied", details={"reason": "x"}, next_actions=[], project="proj")`
  → failure, no `{uid}:teardown` row, one audit row. Red before: a job is enqueued.
- Contract S5: existing `tests/mcp/ops/test_breakglass.py` green.
- Command: `just test-verbose tests/mcp/lifecycle/test_systems_tools.py tests/mcp/ops/test_breakglass.py`.

Steps:
1. Write the three tests; run the command; observe the red failures above.
2. `force_teardown(pool, ctx, *, system_id, reason, resolver: ProviderResolver | None = None)`;
   pass `resolver` to `_teardown_locked(pool, ctx, uid, resolver)`.
3. In `_teardown_locked`, after the `REPROVISIONING` refusal:

```python
        routed = await route_external_boot_teardown(conn, ctx, system, str(uid), None, resolver)
        if routed is not None:
            return routed
        if not await ordinary_mutation_is_fenced(conn, uid):
            denial = await ordinary_teardown_denial(conn, ctx, system, str(uid))
            if denial is not None:
                return denial
```
   then the existing `enqueue_control_teardown(..., recycle=FAILED)`. `ordinary_mutation_is_fenced`
   is imported from `kdive.services.systems.authority_owned`; a preactivation System must skip
   the matrix, which refuses it (existing `test_force_teardown_routes_preactivation_authority_system`).
4. `register`: `force_teardown(..., resolver=resolver)`. Wrapper docstring: add "A System with
   external-boot history gets the authority-marked teardown that systems.teardown enqueues."
   Update the `force_teardown` and module docstrings to match.
5. Green command; `just lint`; `just type`. Commit `fix(ops): route break-glass teardown through
   the external-boot authority (#3007)`.

## Task 3: ADR-0620 amendment

Verification: Mode: task-test-not-applicable — prose record; `just records` validates shape only.
Steps: append `### Amendment (2026-10-01): break-glass teardown routes external-boot history
(#3007)` before `## Consequences`, stating that `ops.force_teardown` now routes as
`systems.teardown`, that the #2966 amendment's producer list now names only the orphaned-System
lane and investigation force-close (#3025), and one rejected alternative (service extraction,
`judgment:`). ADR-0062 §4 needs no note: break-glass still reuses the per-project teardown
mechanics with its own authorization and audit. `git fetch origin main && just records`. Commit
`docs(adr): amend ADR-0620 for break-glass authority teardown (#3007)`.
