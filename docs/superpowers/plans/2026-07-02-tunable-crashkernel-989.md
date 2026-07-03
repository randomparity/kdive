# Implementation plan — Tunable kdump crashkernel reservation per install (#989)

- **Spec:** [`docs/superpowers/specs/2026-07-02-tunable-crashkernel-989.md`](../specs/2026-07-02-tunable-crashkernel-989.md)
- **ADR:** [ADR-0300](../../adr/0300-tunable-crashkernel-reservation.md)
- **Base branch:** `main` · **Feature branch:** `feat/crashkernel-tunable-989`

## Summary

Add an optional, structured `crashkernel` parameter to `runs.install` that tunes the size of the
platform-injected `crashkernel=` reservation (default `256M`) for that install, no rebuild — the
sibling of the ADR-0299 `cmdline` override for the one platform-owned token the free-form override
may not touch. Method-gated (only emitted for `KDUMP`); a request on a non-kdump System is rejected
synchronously; validation is injection-safe (non-empty, no internal whitespace, no `crashkernel=`
prefix) not a size range. Reuses the ADR-0299 ledger re-stage machinery, records the applied value
on the `install` step, and surfaces it as `runs.get data.installed_crashkernel`.

**No schema change, no migration.** The reservation rides the existing `InstallPayload` jsonb and
`install` step-result jsonb.

## Conventions & guardrails (every task)

- Python 3.14, `uv`. TDD: write the failing test first, watch it fail, implement, watch it pass.
- Guardrails, run the recipes individually (CI runs them separately):
  `just lint` · `just type` (whole-tree) · `just test`. Before push run the full `just ci`
  (adds `docs-check`, `adr-status-check`, etc.).
- Absolute imports only; ≤100 lines/function; ≤100-char lines; Google-style docstrings on public
  APIs; no ADR-NNNN strings in agent-facing descriptions (guard: `test_no_adr_leak`).
- After any tool docstring/`Field` change, regenerate the tool reference: `just docs`, then
  `just docs-check` must be clean (memory: generated-doc drift is a separate gate from `just test`).
- The reference implementation to mirror throughout is the **#988 / ADR-0299 `cmdline` override**
  (same files, same shapes). Read each file's existing `cmdline` handling before adding
  `crashkernel` beside it.

## Task 1 — `system_required_cmdline` / `cmdline_for` gain a `crashkernel` keyword

**Where it fits:** the pure cmdline-composition core; everything downstream threads into these two.

**Files:** `src/kdive/services/runs/steps.py`; tests `tests/services/runs/test_cmdline.py`.

**Do:**
- Rename `_KDUMP_CRASHKERNEL = "crashkernel=256M"` → `_DEFAULT_CRASHKERNEL = "256M"` (just the size).
- `system_required_cmdline(method, root_cmdline, *, crashkernel: str | None = None)`: for
  `CaptureMethod.KDUMP`, append `f"crashkernel={crashkernel or _DEFAULT_CRASHKERNEL}"`. Non-kdump
  branches unchanged (no crashkernel token). Token order (console → root → crashkernel/nokaslr)
  preserved.
- `cmdline_for(conn, run, method, *, root_cmdline, override=None, crashkernel: str | None = None)`:
  pass `crashkernel` through to `system_required_cmdline` in both the `override`-set and
  build-baked branches. `override` (ADR-0299) is orthogonal and unchanged.
- Update both docstrings to name the new keyword.

**Tests (write first):**
- `system_required_cmdline(KDUMP, root, crashkernel="512M")` → `"console=ttyS0 root=/dev/vda crashkernel=512M"`.
- `crashkernel=None` → default `256M` (the existing assertions must still hold — do not break them).
- Non-kdump methods with `crashkernel="512M"` emit **no** crashkernel token.
- `cmdline_for` threads crashkernel; orthogonal to `override` (both set → both applied, tokens
  ordered platform-first).

**Acceptance:** existing `test_cmdline.py` assertions stay green; new crashkernel cases pass.

## Task 2 — `InstallPayload.crashkernel` with injection-safe validator

**Where it fits:** the worker-side payload the re-stage enqueues and the handler reads; the
validator is the backstop the tool boundary cannot be trusted to be the only enforcer of.

**Files:** `src/kdive/jobs/payloads.py`; tests `tests/jobs/test_payloads.py`.

**Do:**
- Add `crashkernel: str | None = None` to `InstallPayload` beside `cmdline`.
- Add a `@field_validator("crashkernel")` that, for a non-`None` value: strips; rejects blank
  (`ValueError`); rejects any internal whitespace (`if value.split() != [value]` or a regex on
  `\s`); rejects a value beginning with `crashkernel=` (case-insensitive). Returns the stripped
  value. (These are safety guards, not range validation — a size *or* a range like
  `1G-2G:128M,2G-:256M` must pass.)

**Tests (write first):**
- Accepts `"512M"`, `"1G"`, and a range `"1G-2G:128M,2G-:256M"` (returns stripped).
- Rejects `""`, `"   "`, `"512 M"` (internal space), `"crashkernel=512M"`.
- `crashkernel=None` decodes fine; a bare `{run_id}` / cmdline-only payload still decodes
  (`crashkernel=None`).

**Acceptance:** validator rejects the malformed set and admits the opaque-token set.

## Task 3 — Install handler applies + records the reservation, with a non-kdump backstop

**Where it fits:** the worker executor that composes the boot cmdline and writes the `install`
ledger row.

**Files:** `src/kdive/jobs/handlers/runs/install.py`; tests
`tests/jobs/handlers/test_runs_install.py` (or the existing install-handler test module).

**Do:**
- In the `JobKind.INSTALL` branch, read `install_payload.crashkernel`; the composite/other-kinded
  branch leaves it `None` (unchanged ADR-0299 contract — the composite reads no override).
- Pass `crashkernel=<value>` to `cmdline_for`.
- **Backstop:** after `method` is resolved, if `crashkernel is not None and method is not
  CaptureMethod.KDUMP`, raise `CategorizedError("crashkernel requires a kdump-capture system",
  category=CONFIGURATION_ERROR, details={"reason": "crashkernel_requires_kdump", "method":
  method.value})`. (The tool boundary already rejects this; the backstop covers a hand-crafted
  payload or an accept-then-reprovision skew.)
- Record the applied reservation in the `install` step result:
  `{"system_id": ..., "cmdline": applied_extra, "crashkernel": applied_crashkernel}` where
  `applied_crashkernel` is the payload value (or `None` for default).

**Tests (write first):**
- Handler passes `payload.crashkernel` into the composed cmdline (assert the domain/InstallRequest
  cmdline carries `crashkernel=512M`).
- Records `crashkernel` in the install result; `None` when omitted.
- Raises `crashkernel_requires_kdump` when `crashkernel` set and method is `CONSOLE`/`GDBSTUB`.

**Acceptance:** install result carries the applied reservation; non-kdump backstop fires.

## Task 4 — `StepProgress.installed_crashkernel` + `runs.get` read-back

**Where it fits:** the ledger reader and the `runs.get` view; needed by Task 5's re-stage
comparison and by the agent-facing read-back.

**Files:** `src/kdive/services/runs/steps.py` (`StepProgress`, `step_progress`);
`src/kdive/mcp/tools/lifecycle/runs/common.py` (`_installed_cmdline_data` sibling);
tests `tests/mcp/lifecycle/test_runs_tools.py` (read-back) + `tests/services/runs/` if present.

**Do:**
- Add `installed_crashkernel: str | None = None` to `StepProgress`; in `step_progress`, read it via
  `_optional_str(install_result.get("crashkernel"))`.
- Surface it in the `runs.get` `data` beside `installed_cmdline` — extend/duplicate
  `_installed_cmdline_data` to also emit `"installed_crashkernel": step_progress.installed_crashkernel`
  (only when `step_progress` is not `None`). Keep `null` semantics identical to `installed_cmdline`.

**Tests (write first):**
- `runs.get data.installed_crashkernel` == the last install's applied reservation; `null` before
  first install and when default is in force.

**Acceptance:** read-back reflects the recorded value; existing `installed_cmdline` read-back stays
green.

## Task 5 — `runs.install` boundary: parameter, method-gate, re-stage, audit

**Where it fits:** the agent-facing tool and its re-stage state machine — the heart of the change.

**Files:** `src/kdive/mcp/tools/lifecycle/runs/steps.py` (`install_run`,
`_restage_and_enqueue_install`); `src/kdive/mcp/tools/lifecycle/runs/registrar.py`
(`_register_runs_install`); tests `tests/mcp/lifecycle/test_runs_tools.py`.

**Do:**
- `_register_runs_install(app, pool, resolver)` — thread the `resolver` (available in
  `register_runs_tools`, already passed to sibling registrars). Add a `crashkernel` `Annotated[str
  | None, Field(...)]` parameter. `Field` text: sets the kdump `crashkernel=` reservation size;
  default `256M`; applies only to kdump-capture Systems; iterate without a rebuild; **each install
  fully specifies both `cmdline` and `crashkernel` — omitting either reverts it to its default**
  (see spec §Agent-facing contract). No ADR-NNNN in the text.
- `install_run(pool, ctx, run_id, *, cmdline=None, crashkernel=None, resolver, idempotency_key=None)`:
  - Validate `crashkernel` format synchronously (reuse the same rules as the payload validator via a
    shared helper — extract `validate_crashkernel_token(value) -> str | None` to avoid duplication):
    blank → `crashkernel_blank`; internal whitespace or `crashkernel=` prefix → `crashkernel_malformed`.
  - **Only when `crashkernel` is supplied**, after the existing Run/state/`system_id` checks, fetch
    the System (`SYSTEMS.get`) and resolve the method (`resolver.binding_for_system` +
    `install_method_for`); if not `KDUMP`, return `CONFIGURATION_ERROR`
    `data={"reason":"crashkernel_requires_kdump","method":method.value}`. Map a resolution failure to
    `configuration_error` (do not let it escape as a 500). When `crashkernel` is `None`, skip all of
    this — the path stays byte-unchanged.
  - Thread `crashkernel` into `_restage_and_enqueue_install` and the `InstallPayload`.
- `_restage_and_enqueue_install(conn, ctx, run, cmdline, crashkernel)`:
  - Compute `requested_crashkernel = crashkernel.strip() if crashkernel is not None else None`
    (None = default, matching the recorded-`None`-for-default convention).
  - Extend the re-stage predicate: re-stage when `install == "succeeded"` and
    (`installed_cmdline != requested_cmdline` **or** `installed_crashkernel != requested_crashkernel`).
  - Carry `crashkernel` in `InstallPayload(run_id=..., cmdline=cmdline, crashkernel=crashkernel)`.
  - Fold `crashkernel` into `audit_args` when supplied (one-way digest, mirroring `cmdline`).

**Tests (write first):**
- `runs.install` accepts `crashkernel`, enqueues an `InstallPayload` carrying it.
- Rejects a non-kdump System → `crashkernel_requires_kdump` (+ `method`).
- Rejects blank → `crashkernel_blank`; internal-space / `crashkernel=`-prefixed → `crashkernel_malformed`.
- Re-stage: same crashkernel → no-op (no row delete, no new job); differing crashkernel with
  unchanged cmdline → both ledger rows deleted + fresh install job carrying the new value;
  install(`512M`) then install() [omit] → re-stage back to default.
- `step_in_progress` still rejected while a step is `running`.
- `crashkernel=None` install path enqueues without any System fetch (unchanged behavior — assert no
  regression on the existing install tests).

**Acceptance:** all boundary + re-stage cases pass; existing `runs.install` cmdline tests stay green.

## Task 6 — Composite `runs.build_install_boot` tool-doc note

**Where it fits:** closes the review's finding-1 gap — the KASAN one-shot must not *silently* boot
256M.

**Files:** `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (the
`runs.build_install_boot` `Field`/docstring); tests: the agent-doc/completeness guard if one asserts
on this tool's text.

**Do:** add one sentence to the tool's docstring or `cmdline` `Field`: the one-shot uses the default
`256M` kdump reservation; for a larger reservation (KASAN/large guest) use the granular
`runs.build → runs.install(crashkernel=…) → runs.boot` path. No code/behavior change to the
composite.

**Acceptance:** the note is present and discoverable; no behavior change; completeness guard green.

## Task 7 — Regenerate docs + agent guides

**Files:** `docs/guide/reference/runs.md` (generated); any hand-written guide under `docs/guide/`
that enumerates `runs.install` parameters or install-time knobs.

**Do:**
- Run `just docs` to regenerate the tool reference; confirm `runs.md` now lists the `crashkernel`
  parameter and `data.installed_crashkernel`.
- Grep `docs/guide/` for hand-written mentions of the install cmdline / platform tokens; if a guide
  narrates the install knobs, add crashkernel beside the ADR-0299 cmdline mention.
- `just docs-check` clean.

**Acceptance:** `just docs-check`, `just docs-links`, `just docs-paths` all clean.

## Task 8 — Live proof (gated) + full guardrail sweep

**Files:** `tests/**` under the `live_vm` marker (mirror the ADR-0299 acceptance live test if one
exists); this host runs KVM/libvirt directly (memory: `host-runs-live-vm-tests`).

**Do:**
- Add a `live_vm`-gated test: on a kdump-provisioned local System,
  `runs.install(crashkernel="512M") → runs.boot`, assert the booted domain `<cmdline>` carries
  `crashkernel=512M` (not `256M`) and `runs.get data.installed_crashkernel == "512M"`. Not a PR
  gate.
- Run the full `just ci`; fix every warning (zero-warnings policy).

**Acceptance:** `just ci` green; the live test passes on the KVM host (record the proof).

## Rollback / cleanup

- No migration, so rollback is a pure `git revert` of the branch — no schema state to unwind.
- The `install` step-result `crashkernel` key is additive JSON; an older reader ignores it, and
  `step_progress` treats a missing key as `None` (default), so a mixed-version worker/server is
  forward/backward compatible.
- `InstallPayload.crashkernel` defaults to `None`, so a pre-#989 serialized install job decodes
  unchanged.

## Task order & parallelism

Strict dependency chain: **1 → 3, 1 → 5**; **2 → 3, 2 → 5**; **4 → 5** (re-stage reads
`installed_crashkernel`). Tasks 1 and 2 are independent and may go in parallel. Task 4 depends on
Task 3's recorded key. Tasks 6, 7 follow the code; Task 8 is last. Recommended serial order:
1, 2, 3, 4, 5, 6, 7, 8 — one commit per task, guardrails green at each.
