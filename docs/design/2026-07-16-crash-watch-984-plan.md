# Implementation plan — out-of-band crash-signature console watch (#984)

Derived from `2026-07-16-crash-watch-984.md` and
[ADR-0367](../adr/0367-out-of-band-crash-watch.md).

- **Branch:** `feat/stress-repro-984` (off `origin/main`).
- **Base:** `main`.
- **Guardrails (run before every commit):** `just lint`, `just type` (whole tree), targeted
  `uv run python -m pytest <files> -q`; `just docs-links` + `just resources-docs-check` for the
  docs; the full `just ci` before push (it runs `lint type … check-mermaid docs-links docs-paths
  adr-status-check docs-check … schema-guard … resources-docs-check … test` as individual gates).
- **Migration:** one forward-only migration `0069_watch_for_crash_job_kind.sql` (byte-immutable
  once committed, ADR-0015). No table/column change.
- **New dependency:** none.
- **Generated artifacts (CI-gated, must be regenerated + committed):** adding a tool to
  `exposure.py` changes the **RBAC tool-visibility matrix** (`just rbac-matrix` →
  `docs/guide/safety-and-rbac.md`, verified by `rbac-matrix-check`, which `just test` runs) and
  the **doc-resource snapshots** (`just resources-docs` → `_content/` snapshots, verified by
  `resources-docs-check`). Never hand-edit a generated file that carries a "do not edit by hand"
  marker — edit its source and regenerate (see Task 6).
- **Worker execution model:** handlers run under **autocommit** on a dedicated dispatch
  connection with a background heartbeat renewing the lease (`jobs/worker.py`; 30+ min handlers
  are explicitly supported). The watch's up-to-300s poll therefore holds **no** open transaction
  and stays within the long-handler envelope — do not wrap the poll in a transaction; only the
  short reads (`SYSTEMS.get`, binding resolve, audit) touch the DB.
- **TDD:** each task writes the test(s) first, watches them fail, then implements. Suggested
  order 1→7; 1 and 2 are independent, 3 depends on 1+2, 4 on 3, 5 on 2, 6 on 5, 7 on 2+5.

> **Agent-surface guardrail (applies to Tasks 5–6):** the `@app.tool` **wrapper docstring +
> `Field(description=…)`** are the only agent-facing text (FastMCP serializes nothing else).
> They must name every verdict field and outcome and must **not** cite ADR/issue numbers
> (`test_no_adr_leak` fails on a leaked `ADR-` / `#NNN` in the tool schema). Put design rationale
> in the module docstring / this plan, never in the wrapper.

---

## Task 1 — Promote the crash matcher to a public single-source helper

**Where it fits:** Spec §"Crash-signature matcher — reuse"; acceptance #5. Both boot readiness
and the watch must share one `_CRASH_SIGNATURE` definition; the watch cannot import a private
name.

**Files:**
- `src/kdive/domain/lifecycle/crash_signatures.py` — **relocate** `_CRASH_SIGNATURE` (the readiness
  regex) here and add `def first_crash_signature(text: str) -> re.Match[str] | None` returning
  `_CRASH_SIGNATURE.search(text)`. The domain layer is the boundary-safe home both boot readiness
  (a provider) and the watch (a jobs handler) can import — a jobs handler may not import
  `providers/local_libvirt` internals (provider-boundary guard).
- `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` — drop the inline
  `_CRASH_SIGNATURE`; import and call `first_crash_signature` in `classify_console`.
- `tests/providers/local_libvirt/test_install.py` — the existing `classify_console` test module;
  new `first_crash_signature` cases.

**Test first:**
- `first_crash_signature` returns a match for each signature family (`Kernel panic`, `BUG:`,
  `Oops:`, `general protection fault`, `unable to handle kernel`, `KASAN:`, `KFENCE:`,
  `detected stall`) and `match.group(0)` is the matched literal.
- Word-boundary cases hold (`DEBUG:` / `aBUG:` do **not** match — the existing `(?<![A-Za-z])`
  behavior is preserved).
- Non-crash text returns `None`.
- Existing `classify_console` tests remain green (pure refactor, no behavior change).

**Acceptance:** `rg -n "_CRASH_SIGNATURE\s*=" src/` shows exactly one definition; `classify_console`
and the new helper both route through it; `just type` + readiness tests green.

**Rollback:** revert the file; the helper has no other callers yet.

---

## Task 2 — Migration + `JobKind` + payload contract

**Where it fits:** Spec §Persistence; acceptance #8. The queue must accept the new kind and
decode its payload.

**Files:**
- `src/kdive/db/schema/0069_watch_for_crash_job_kind.sql` (new) — drop-and-recreate
  `jobs_kind_check` widened with `'watch_for_crash'`, header comment in the 0057 style
  (forward-only, constraint-name stable for the SQL↔enum tie).
- `src/kdive/domain/operations/jobs.py` — add `WATCH_FOR_CRASH = "watch_for_crash"` to `JobKind`;
  add it to `CONTRIBUTOR_CANCELABLE_JOB_KINDS` (a contributor cancels its own watch).
- `src/kdive/jobs/payloads.py` — add `WATCH_DEFAULT_DEADLINE_S = 60.0` and
  `WATCH_MAX_DEADLINE_S = 300.0` module constants (the **single source** for the caps — both the
  tool and the handler import them from here, so the MCP tool never imports the worker-handler
  module and pulls provider deps into its import graph). Add `WatchForCrashPayload(SystemPayload)`
  with `deadline_s: float` and a `field_validator` rejecting non-finite / non-positive
  `deadline_s` and clamping `> WATCH_MAX_DEADLINE_S` to the cap (a worker-side backstop; the tool
  boundary clamps/rejects with per-reason codes first). Register in
  `_ACTIVE_PAYLOAD_MODELS[JobKind.WATCH_FOR_CRASH]` and add to `_ActivePayloadModel` /
  `ActivePayloadModel` unions.
- `tests/db/test_migration_0069_watch_for_crash.py` (new, mirror
  `test_migration_0057_check_ssh_reachable.py`).
- `tests/db/test_migrate.py` — should pick up the new kind via the SQL↔enum tie automatically;
  confirm it still passes (adjust the expected-kinds set if it is hard-coded).
- `tests/jobs/test_payloads.py` (or equivalent) — payload round-trip + validation cases.

**Test first:**
- Migration: after applying through 0069, `jobs_kind_check` admits `'watch_for_crash'` and still
  rejects a bogus kind; the constraint name is unchanged.
- `dump_payload(JobKind.WATCH_FOR_CRASH, WatchForCrashPayload(system_id=<uuid>, deadline_s=60))`
  round-trips; `deadline_s=0`, negative, `inf`, `nan` are rejected with `PayloadValidationError`;
  a non-UUID `system_id` is rejected.
- `CONTRIBUTOR_CANCELABLE_JOB_KINDS` contains `WATCH_FOR_CRASH`.

**Acceptance:** `just type`; migration + payload tests green; `test_migrate.py` green.

**Rollback:** migrations are append-only and byte-immutable once committed — do **not** rewrite
0069 after it merges; a later fix is a new migration. Before merge it may be edited freely.

---

## Task 3 — The watch core (pure, injectable) + worker handler

**Where it fits:** Spec §"Job handler behavior", §"Result contract", §"The missed-crash window".
This is the correctness core and the bulk of the tests.

**Files:**
- `src/kdive/jobs/handlers/control/watch_for_crash.py` (new):
  - Handler-internal tuning constants: `POLL_INTERVAL_S = 1.0`, `CONTEXT_LINES = 3`,
    `MATCHED_MAX_BYTES = 4096`. The deadline caps (`WATCH_DEFAULT_DEADLINE_S`,
    `WATCH_MAX_DEADLINE_S`) are imported from `jobs.payloads` (single source; the tool imports the
    same, so no MCP→handler import edge).
  - `@dataclass(frozen=True, slots=True) class WatchVerdict` with fields
    `outcome: Literal["fired","not_fired"]`, `fired: bool`, `signature: str | None`,
    `matched: str | None`, `elapsed_s: float`, `observed_at: str`; `to_json() -> str` emits compact
    JSON (`json.dumps(..., separators=(",", ":"))`) omitting `None` `signature`/`matched` on the
    `not_fired` shape.
  - **Pure core** (no I/O, fully injectable — the unit-test seam):
    ```
    async def watch_console_for_crash(
        read_console: Callable[[], Awaitable[bytes]],
        sleep: Callable[[float], Awaitable[None]],
        clock: Callable[[], float],           # monotonic
        redact: Callable[[str], str],
        now: Callable[[], str],
        *, mark: int, deadline_s: float,
        poll_interval: float, context_lines: int, max_bytes: int,
    ) -> WatchVerdict
    ```
    Loop: snapshot suffix `body[mark:]`; if `first_crash_signature` matches, build the redacted
    bounded slice (matched line ± `context_lines`, then truncate to `max_bytes`) and return
    `fired`. Handle `len(body) < mark` → reset `mark = 0` (truncation guard, logged). At the
    deadline return `not_fired`. `elapsed_s = clock() - start`. **No liveness probe** — the agent
    holds the authoritative SSH-drop signal (spec §The missed-crash window); a virsh probe would
    also cross the provider boundary.
  - `watch_for_crash_handler(conn, job, *, resolver, secret_registry) -> str | None`: load
    `WatchForCrashPayload`; `SYSTEMS.get`; raise `CategorizedError(CONFIGURATION_ERROR,
    reason="system_not_ready")` if not `READY`; resolve `binding` and raise
    `reason="not_local_libvirt"` if not local-libvirt; snapshot
    `mark = len(read_console_log(console_log_path(system_id)))`; build real seams
    (`read_console` via `asyncio.to_thread(read_console_log, …)`; `redact` via
    `Redactor(registry=secret_registry).redact_text`; `observed_at` from module-level
    `datetime.now(UTC)`); call the core; audit the outcome; return `verdict.to_json()`.
    **Boundary note:** the handler imports `first_crash_signature` from
    `domain.lifecycle.crash_signatures` (Task 1 relocation), never from `providers/local_libvirt`.
  - `register_handlers(registry, *, resolver, secret_registry)`.
- `tests/jobs/handlers/control/test_watch_for_crash.py` (new).

**Test first (deterministic, no VM — inject fake `read_console`/`sleep`/`clock`/`redact`/`now`):**
- **Fired, first hit past mark:** a console whose suffix gains `Kernel panic - not syncing` on
  poll N returns `fired`, `signature=="Kernel panic"`, `elapsed_s` within `[0, deadline+poll]`,
  and `matched` contains the panic line.
- **Deterministic first hit:** two signatures present; the earlier (lower offset) one is reported.
- **Pre-mark panic ignored:** `mark` set past an existing panic line → not matched → `not_fired`.
- **Not-fired:** no signature by the deadline → `outcome="not_fired"`, `fired is False`,
  `elapsed_s ∈ [deadline, deadline+poll]`.
- **Truncation guard:** console length drops below `mark` mid-watch, then a panic appears →
  matched (mark reset to 0), no crash.
- **Redaction applied:** a registered secret in the context lines is masked in `matched` (inject a
  `secret_registry` with a known secret).
- **Context + byte bounds:** `matched` is at most `2*CONTEXT_LINES+1` lines and ≤ `MATCHED_MAX_BYTES`.
- **Non-halting signature:** `detected stall` fires (guest not exited) — proves the watch does not
  assume a halt.
- **Handler gates:** non-`READY` System → `CategorizedError` `reason="system_not_ready"`;
  non-local-libvirt binding → `reason="not_local_libvirt"`.
- **Audit recorded:** the handler records an audit event (tool `control.watch_for_crash`,
  `object_kind="systems"`, transition carrying the outcome) — assert an audit row exists after a
  fired and a not-fired run (mirror `diagnostic_sysrq`'s audit test).

**Acceptance:** `just lint`, `just type`, the new test module green; the core has no direct I/O
(all seams injected).

**Rollback:** delete the new module + test; no other file imports it until Task 4.

---

## Task 4 — Register the handler in the worker assembly

**Where it fits:** Spec §Concurrency (the job must have a registered handler); acceptance #1
(enqueue → worker runs it).

**Files:**
- `src/kdive/jobs/assembly.py` — add `_watch_for_crash_handler_registrar(*, resolver,
  secret_registry)` (mirror `_diagnostic_sysrq_handler_registrar`) and append it to the tuple in
  `build_handler_registrars`.
- `tests/jobs/…` — the existing "every active job kind has a registered handler" coverage test
  (find it: `rg -n "ACTIVE_JOB_KINDS|register" tests/jobs/`) must stay green; if it enumerates
  kinds explicitly, add `WATCH_FOR_CRASH`.

**Test first:** a test asserting `build_handler_registry(...).has(JobKind.WATCH_FOR_CRASH)` (or the
existing coverage test) passes.

**Acceptance:** worker handler-coverage test green; `just type`.

**Rollback:** remove the registrar entry.

---

## Task 5 — The `control.watch_for_crash` MCP tool

**Where it fits:** Spec §"Tool: control.watch_for_crash"; acceptance #1, #4. Synchronous
admission + enqueue.

**Files:**
- `src/kdive/mcp/tools/lifecycle/control/registrar.py`:
  - `_WATCH_FOR_CRASH_KIND = "control.watch_for_crash"`.
  - `async def watch_for_crash_system(pool, ctx, *, system_id, deadline_s, resolver,
    idempotency_key=None) -> ToolResponse`: `_as_uuid`; `SYSTEMS.get`; project-scope →
    `_config_error` (absent-shaped) if not in `ctx.projects`; `require_role(ctx, project,
    CONTRIBUTOR)`; validate `deadline_s` finite/positive → `_config_error` else **clamp to
    `[.., WATCH_MAX_DEADLINE_S]`** (imported from `jobs.payloads` — single source, no
    MCP→handler import edge);
    `binding.kind is LOCAL_LIBVIRT` else `_config_error(reason="not_local_libvirt")`; `READY`
    else `_config_error(data={"current_status": …})`; enqueue `JobKind.WATCH_FOR_CRASH` with
    `WatchForCrashPayload(system_id, deadline_s=clamped)`, dedup
    `f"{system_id}:watch_for_crash:{idempotency_key or uuid4()}"`, default dispatch lane;
    `job_envelope`; wrap in `keyed_mutation`.
  - `@app.tool(name="control.watch_for_crash", annotations=_docmeta.mutating(),
    meta=_docmeta.maturity_meta("implemented"))` wrapper. **Wrapper docstring names:** what it
    does (watches the READY local-libvirt guest's serial console out-of-band for a kernel-crash
    signature until `deadline_s`, returns on the first hit); that the **agent drives its own
    reproducer loop over SSH** — this only watches the console, which survives the panic that
    drops SSH; the returned `refs.result` verdict fields (`outcome` one of `fired`/`not_fired`,
    `fired`, `signature`, `matched`, `elapsed_s`); that a `not_fired` paired with a dropped
    reproducer-SSH means "the crash was outside the watched window — read the full console";
    contributor-level, non-destructive; enqueues a job → poll `jobs.wait`. No ADR/issue numbers.
- `src/kdive/mcp/exposure.py` — add `"control.watch_for_crash": _CONTRIBUTOR` in the `# control`
  block; if `CLASSIFIED_TOOLS`/`PUBLIC_TOOLS` are hand-maintained sets rather than derived from
  the scope map, add the tool to the correct one (a `_CONTRIBUTOR` tool is classified).
- **Regenerate the RBAC matrix:** `just rbac-matrix` (rewrites `docs/guide/safety-and-rbac.md`
  from the exposure map) and commit it; `just rbac-matrix-check` must be green (it is gated by
  `just test`).
- `tests/mcp/lifecycle/test_control_tools.py` — tool cases.
- `tests/mcp/…/test_exposure*.py` — scope entry (find with `rg -n "check_ssh_reachable|control.power" tests/`).

**Test first (handler tested directly, no transport):**
- Happy path (READY local-libvirt, contributor) → `queued` envelope with a `job_id`; a
  `watch_for_crash` job is enqueued with the clamped `deadline_s` and correct dedup key.
- Viewer (no contributor) → `RoleDenied`/authorization-denied.
- Non-READY → `configuration_error` with `current_status`.
- Non-local-libvirt → `configuration_error` `reason="not_local_libvirt"`.
- `deadline_s` = `nan`/`inf`/≤0 → `configuration_error`; `deadline_s` > MAX → clamped (assert the
  enqueued payload's value == MAX).
- System in ungranted project → not-found-shaped `configuration_error` (no existence leak).
- Idempotency replay: same `idempotency_key` returns the prior envelope (`keyed_mutation`).
- `exposure.required_scopes("control.watch_for_crash")` == `_CONTRIBUTOR`.

**Acceptance:** `just lint`, `just type`, control-tool + exposure tests green; `test_no_adr_leak`
green (no ADR ref in the tool schema).

**Rollback:** remove the tool + handler + exposure entry.

---

## Task 6 — Agent-facing docs + resource content

**Where it fits:** Spec acceptance #7. The tool must be discoverable and race-debugging.md must
route to it.

**Respect the doc-generation pipeline.** `gen_doc_resources.py` maps a **canonical source** doc
to a committed **snapshot** under `_content/`; `resources-docs-check` diffs them. Do **not**
hand-edit a `_content/` file if it is the generated snapshot — first identify the source in the
generator's entry registry (`rg -n "source|content_file|_ENTRIES|toolsets-control" scripts/gen_doc_resources.py`),
edit the **source**, add the tool to the registry if the control toolset is not already covered,
then run `just resources-docs` to regenerate and commit both.

**Files:**
- The **canonical source** for the control toolset content (per the generator registry) — add a
  `control.watch_for_crash` section mirroring the `diagnostic_sysrq` entry (agent-facing prose:
  what it observes, that the agent drives the reproducer loop over SSH, the verdict outcomes). No
  ADR/issue numbers.
- The **agent-index** source — reference the tool in the reproduce-and-capture loop.
- `docs/guide/toolsets/control.md` (if the tool list is enumerated there and not generated) —
  add the tool.
- `docs/operating/race-debugging.md` — Route 3: replace "A repeat-until-crash-signal primitive is
  tracked separately (#984); until it lands, the loop is guest-side SSH." with a pointer to
  `control.watch_for_crash` (start the watch, drive the reproducer over SSH, poll `jobs.wait`;
  the watch catches the panic on the console after SSH drops). Keep the "panic drops your SSH
  channel" paragraph; the watch is the tool that reads the durable console.

**Regenerate + verify:** `just resources-docs` (regenerate snapshots), then
`just resources-docs-check`, `just docs-links`, `just docs-check`, `just docs-paths` green. If
the RBAC matrix (Task 5) also lists per-tool docs, confirm `just rbac-matrix-check` too.

**Acceptance:** all doc guardrails green; the tool appears in the control toolset content; Route 3
names the tool.

**Rollback:** revert the doc edits.

---

## Task 7 — Cancel + list coverage for the new kind

**Where it fits:** Spec §Concurrency (cancelable); ensures the fail-closed cancel gate and the
`jobs.list` kind filter treat `watch_for_crash` correctly.

**Files:**
- `tests/mcp/jobs/test_jobs_tools.py` — a contributor can `jobs.cancel` a queued
  `watch_for_crash` (it is in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`); `jobs.list(kind=watch_for_crash)`
  is accepted (not a retired kind).

**Test first:** the two cases above.

**Acceptance:** jobs-tool tests green.

**Rollback:** none (test-only).

---

## Final gate

Run the **full** `just ci` before push (individual gates: `lint`, `type`, `lock-check`,
`lint-shell`, `lint-ansible`, `test-ansible`, `lint-workflows`, `check-mermaid`, `docs-links`,
`docs-paths`, `adr-status-check`, `docs-check`, `config-docs-check`, `config-guard`,
`env-docs-check`, `schema-guard`, `container-arch-check`, `resources-docs-check`,
`chart-version-check`, `test`). `schema-guard` covers the new migration; `resources-docs-check`
covers the toolset content; `test` runs the unit/service suite (the `live_vm` markers stay gated).
