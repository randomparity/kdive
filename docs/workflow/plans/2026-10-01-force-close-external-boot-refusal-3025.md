# Plan: `investigations.close force=true` refuses external-boot history (#3025)

Goal: force-close refuses, before any write, when a bound live System has an external-boot
activation row, naming the System and `systems.teardown`. Spec:
[`2026-10-01-force-close-external-boot-refusal-design.md`](../specs/2026-10-01-force-close-external-boot-refusal-design.md).

Architecture: one activation read per bound live System inside `_couple_bound_systems`
(`src/kdive/services/investigations/lifecycle.py`), raising a new typed
`InvestigationServiceError` reason; the MCP close adapter maps it to `conflict`.
Tech stack: Python 3.14, psycopg async, pytest with disposable Postgres (Docker).

Expected implementation size: 110–170 changed lines (M) — service check and reason (~30), MCP
mapping (~15), wrapper text (~10), two DB-backed tests and one contract assertion (~70), ADR
amendment (~20).

## Global Constraints

- Ruff line length 100; lint `E,F,I,UP,B,SIM`; `ty` strict, whole tree (`just type`).
- No new dependency, ADR, or migration. ADR-0620 is accepted: append one dated
  `### Amendment (2026-10-01): ... (#3025)` block only; never rewrite existing lines.
- Do not touch `src/kdive/jobs/service_operations.py`, `src/kdive/services/allocation/release.py`,
  `src/kdive/services/external_boot/admission.py`, or `mcp/tools/lifecycle/systems/admin.py`.
- Prose: plain; no "critical", "robust", "comprehensive", "elegant". No ADR references in the
  agent-facing tool schema (`test_close_wrapper_contract_describes_force_refusal`).
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, `just records`
  (after `git fetch origin main`), pre-push `just ci > <file> 2>&1 < /dev/null`.

## File map

| File | Change |
|---|---|
| `src/kdive/services/investigations/common.py` | add `EXTERNAL_BOOT_TEARDOWN_REQUIRED` reason |
| `src/kdive/services/investigations/lifecycle.py` | activation read + refusal in `_couple_bound_systems` |
| `src/kdive/mcp/tools/lifecycle/investigations/lifecycle.py` | map the reason to `conflict` with next actions |
| `src/kdive/mcp/tools/lifecycle/investigations/registrar.py` | `force` field and docstring text |
| `tests/mcp/lifecycle/test_investigations_tools.py` | refusal tests; contract assertion |
| `docs/adr/0620-authority-owned-system-teardown.md` | append amendment |

## Task 1: Force-close refusal

Verification:

- Contract: force-close refuses a bound System with external-boot history, all-or-nothing.
  Mode: focused-test. Test: `test_close_force_refuses_external_boot_bound_system` parametrized
  over an abandoned cleanup-complete activation and an `active` one. Red: `resp.status ==
  "closed"` (today the close succeeds and enqueues `{sid}:teardown`). Green:
  `uv run python -m pytest tests/mcp/lifecycle/test_investigations_tools.py -q -k "force"`.
- Contract: pre-first-activation authority System is not refused. Mode: focused-test. Test: the
  existing `test_close_force_routes_each_system_by_authority_ownership`, unchanged; green under
  the same command.
- Contract: agent-facing `force` text names `systems.teardown`. Mode: focused-test. Test:
  `test_close_wrapper_contract_describes_force_refusal` gains
  `assert "systems.teardown" in force_field`. Red: assertion fails on today's text.

Steps:

1. Test seed. In the test module, add:

   ```python
   async def _seed_external_boot_bound_system(
       pool: AsyncConnectionPool, inv_id: str, state: ExternalBootActivationState
   ) -> UUID:
       """Seed an activation (with its own System and Run) and bind that System to ``inv_id``."""
       async with pool.connection() as conn:
           seeded = await seed_activation(
               conn, state=state, cleanup_complete=state is ExternalBootActivationState.ABANDONED
           )
           await conn.execute(
               "UPDATE systems SET investigation_id = %s WHERE id = %s",
               (inv_id, seeded.system_id),
           )
       return seeded.system_id
   ```

   `seed_activation` is `tests/services/external_boot/conftest.py::seed_activation(conn, *,
   state, cleanup_complete=False, ready_reservation=False, system_id=None, run_id=None)`; it
   inserts a `ready` System in project `proj`.
2. Write the parametrized test: seed an `OPEN` investigation, the external-boot System, and an
   ordinary `READY` System; close with `_ctx(Role.ADMIN)`, `force=True`. Assert:
   `resp.error_category == "conflict"`, `resp.data["reason"] ==
   "external_boot_system_teardown_required"`, `str(sid) in resp.data["external_boot_systems"]`,
   `str(sid) in resp.detail`, `"systems.teardown" in resp.suggested_next_actions`,
   `await _teardown_dedup_keys(pool) == []`, markers `state == "open"`, both cleanup marks
   `None`, and zero `audit_log` rows with `tool = 'investigations.close'` for the investigation.
   Run it: red as stated above.
3. `common.py`: add `EXTERNAL_BOOT_TEARDOWN_REQUIRED = "external_boot_system_teardown_required"`
   to `InvestigationErrorReason` (alphabetical position after `BOUND_SYSTEMS_LIVE`).
4. `lifecycle.py` (service): module constant
   `_EXTERNAL_BOOT_ACTIVATIONS = ExternalBootActivationRepository()` (from
   `kdive.db.external_boot_activations`). In `_couple_bound_systems`, after the reprovisioning
   refusal and before the enqueue loop:

   ```python
   external_boot = [
       str(system_id)
       for system_id in live
       if await _EXTERNAL_BOOT_ACTIVATIONS.get_latest_for_system(conn, system_id) is not None
   ]
   if external_boot:
       raise InvestigationServiceError(
           object_id=str(uid),
           reason=InvestigationErrorReason.EXTERNAL_BOOT_TEARDOWN_REQUIRED,
           detail=(
               f"cannot force-close: {len(external_boot)} bound System(s) have external-boot "
               f"history ({', '.join(external_boot)}); the provider-host authority owns their "
               "domains, so tear each down with systems.teardown, then close"
           ),
           data={"external_boot_systems": list(external_boot)},
       )
   ```

   Update the docstring to name the three refusals.
5. `mcp/.../investigations/lifecycle.py`: in `close_investigation`'s `except`, before
   `investigation_error_response`, return for the new reason
   `ToolResponse.failure(exc.object_id, ErrorCategory.CONFLICT, detail=exc.detail,
   suggested_next_actions=["systems.teardown", "systems.get"], data={"reason": exc.reason.value,
   **exc.data})`.
6. `registrar.py`: extend the `force` description and the wrapper docstring: a bound System with
   external-boot history makes `force` refuse; tear it down with `systems.teardown`, then close.
   Add the contract assertion; run the focused command: green. Run `just lint`, `just type`.
7. Commit: `fix(investigations): refuse force-close of external-boot Systems (#3025)`.

## Task 2: ADR-0620 amendment

Verification: Mode: task-test-not-applicable — an append-only prose record with no executable
consumer; `just records` checks only its shape.

Steps:

1. Append `### Amendment (2026-10-01): investigation force-close refuses external-boot history
   (#3025)` after the #3016 amendment, before `## Consequences`, stating spec Design items 2–4
   and 7, with rejected alternatives: route with a resolver (operator decision; judgment: a
   resolver in `JobOperations`), refuse in `enqueue_control_teardown` (judgment: affects the
   orphaned lane), do nothing (verified: worker refusal at `jobs/handlers/systems.py`).
2. `git fetch origin main && just records`; commit
   `docs(adr): record the force-close external-boot refusal (#3025)`.
