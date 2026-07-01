# Implementation plan — `control.diagnostic_sysrq` (#925)

Derived from `docs/design/2026-06-30-sysrq-diagnostic-capture-925.md` and
[ADR-0285](../../adr/0285-sysrq-diagnostic-capture.md). Numbers: **ADR-0285**, **migration
0055**. TDD throughout: failing test first, confirm it fails for the right reason, minimal
impl, focused test + guardrails green, refactor green.

## Conventions and guardrails (apply to every task)

- Python 3.14, `uv`. Absolute imports only. Ruff line length 100, lint `E,F,I,UP,B,SIM`.
  Google-style docstrings on public APIs. `ty` strict (whole tree incl. `tests/`).
- **The `@app.tool` wrapper docstring + `Field(description=...)` is the agent-facing
  contract** (AGENTS.md): name every returned `data`/`refs` field and constraint there.
- Return `ToolResponse` with the most specific `ErrorCategory`; never invent strings.
- Secrets/console output pass the `Redactor` before persistence or any snippet.
- Guardrail commands (run before each commit): `just lint`, `just type`, and the focused
  test file(s); before pushing run the **full** `just ci` set (`lint`, `type`, `lint-shell`,
  `lint-workflows`, `check-mermaid`, `test`) plus `just docs-check`, `just rbac-matrix` check,
  `just resources-docs-check`, `just adr-status-check`.
- Single feature branch `feat/sysrq-diagnostic-925`; sequential commits, one logical change
  each, imperative subject ≤72 chars, `Co-Authored-By: Claude Opus 4.8 (1M context)` trailer.
- Tasks are ordered by dependency; later tasks import symbols earlier tasks create.

## Task 1 — Domain: `SysRqCommand` allowlist enum + `JobKind`/payload

**Where:** the persistence + allowlist foundation everything else builds on.

**Files:** `src/kdive/domain/operations/sysrq.py` (new), `src/kdive/domain/operations/jobs.py`,
`src/kdive/jobs/payloads.py`, `tests/domain/test_sysrq.py` (new), `tests/jobs/test_payloads.py`.

**Do:**
- New `src/kdive/domain/operations/sysrq.py`: `class SysRqCommand(StrEnum)` with the seven
  friendly values (`show_task_states`, `show_blocked_tasks`, `show_memory`, `show_locks`,
  `show_registers`, `show_backtrace_all_cpus`, `show_timers`) and a
  `SYSRQ_TRIGGERS: dict[SysRqCommand, str]` mapping each to its magic-SysRq char
  (`t/w/m/d/p/l/q`). Add a `trigger` property or a `parse(value: str) -> SysRqCommand` helper
  that raises `CategorizedError(CONFIGURATION_ERROR)` on an unknown value with `data` listing
  the allowed values (and, for a known-destructive letter/name like `c`/`crash`/`b`/`reboot`,
  remediation naming `control.force_crash`).
- `jobs.py`: add `JobKind.DIAGNOSTIC_SYSRQ = "diagnostic_sysrq"`. Do **not** add it to
  `DESTRUCTIVE_JOB_KINDS`.
- `payloads.py`: add `SysRqPayload(system_id: str, command: str)` following the existing
  payload dataclass/`load_payload` pattern.

**TDD / acceptance:**
- Test each friendly value maps to the expected trigger char; unknown value → `CONFIGURATION_ERROR`
  with allowed-values data; a destructive name → remediation names `control.force_crash`.
- Update any JobKind-exhaustiveness test (`tests/domain/test_models.py`,
  `tests/jobs/test_payloads.py`) that enumerates kinds; a new kind must not break them.
- `SysRqPayload` round-trips through `load_payload`.

## Task 2 — Migration 0055: widen `jobs_kind_check`

**Where:** persistence must admit the new kind before any enqueue path is exercised end-to-end.

**Files:** `src/kdive/db/schema/0055_diagnostic_sysrq_job_kind.sql` (new); the schema test that
asserts the applied CHECK matches `JobKind` (find via `rg "jobs_kind_check" tests`).

**Do:** copy `0053_console_rotate_job_kind.sql`'s drop-and-recreate shape; the new CHECK lists
every current kind **plus** `'diagnostic_sysrq'`. Header comment: additive/forward-only
(ADR-0015), constraint name stable, cites #925.

**TDD / acceptance:** the migration applies on a fresh DB (testcontainers) and a
`diagnostic_sysrq` job row inserts; the SQL↔enum tie test passes. Requires Docker (skips
locally if absent; CI sets `KDIVE_REQUIRE_DOCKER=1`).

## Task 3 — Control port method + local-libvirt `sendKey`; remote stub

**Where:** the provider seam that actually injects the keystroke.

**Files:** `src/kdive/providers/ports/lifecycle.py` (Controller Protocol),
`src/kdive/providers/local_libvirt/lifecycle/control.py`,
`src/kdive/providers/remote_libvirt/lifecycle/control.py`,
`tests/providers/local_libvirt/test_control.py`,
`tests/providers/remote_libvirt/lifecycle/test_control.py`.

**Do:**
- `Controller` Protocol: add `diagnostic_sysrq(self, domain_name: str, trigger: str) -> None`
  with a Google-style docstring naming raised categories (`CONTROL_FAILURE` for absent domain
  / libvirt fault).
- Local-libvirt `LocalLibvirtControl`: implement via `domain.sendKey(libvirt.VIR_KEYCODE_SET_LINUX,
  HOLDTIME_MS, [KEY_LEFTALT, KEY_SYSRQ, keycode_for(trigger)], 3, 0)`. Add a
  `_SYSRQ_KEYCODES: dict[str, int]` (trigger char → Linux input keycode:
  `t=20,w=17,m=50,d=32,p=25,l=38,q=16`) plus `KEY_LEFTALT=56, KEY_SYSRQ=99` and a
  `HOLDTIME_MS` constant. Extend the `_LibvirtDomain` Protocol with `sendKey`. Map
  `libvirt.libvirtError` (lookup or sendKey) to `CONTROL_FAILURE` via the existing
  `_control_failure` helper. A trigger char not in `_SYSRQ_KEYCODES` is a programming error
  (the tool validated it) — raise a clear error.
- Remote-libvirt `Controller`: add `diagnostic_sysrq` raising `CategorizedError(CONTROL_FAILURE,
  reason="not_supported")` for Protocol conformance (the tool never routes here).

**TDD / acceptance:**
- Fake libvirt domain records the `sendKey` args; assert codeset `VIR_KEYCODE_SET_LINUX` and
  the exact `[56, 99, <keycode>]` list for each command.
- Absent domain (lookup raises) → `CONTROL_FAILURE`; sendKey raises → `CONTROL_FAILURE`.
- Remote stub raises `CONTROL_FAILURE`.
- `ty` confirms both control classes still satisfy the widened `Controller` Protocol.

## Task 4 — Capture core (pure, deterministic)

**Where:** the injectable, time-free settle-poll used by the worker handler.

**Files:** `src/kdive/jobs/handlers/diagnostic_sysrq.py` (new — core + handler in Task 5),
`tests/jobs/test_diagnostic_sysrq.py` (new).

**Do:** a pure async `capture_console_delta(read_console, inject, sleep, *, mark, seam_overlap,
poll_interval, max_polls, settle_polls) -> CaptureResult` where `read_console`/`inject` are
awaitables and `sleep` is injected. Returns the redacted-input raw bytes slice
`[max(0, mark - seam_overlap):]` **and** an exit reason (`stabilized` | `hit_bound` |
`no_output`). `no_output` when the final length ≤ `mark`. Named constants
`MAX_POLLS`/`POLL_INTERVAL`/`SETTLE_POLLS`/`SEAM_OVERLAP`/`HOLDTIME_MS` live at module top.

**TDD / acceptance (no real time, scripted `read_console`):**
- Growth then stable → returns the delta from `mark`, exit `stabilized`.
- Still growing at the bound → exit `hit_bound`, delta returned.
- No growth → exit `no_output`, empty delta.
- A read sequence where a secret straddles `mark`: the returned raw slice includes the
  `seam_overlap` region before `mark` (so downstream redaction sees the whole token).

## Task 5 — Worker handler + registration

**Where:** worker-owned execution: lock-snapshot → inject → lock-free poll → redact →
lock-store.

**Files:** `src/kdive/jobs/handlers/diagnostic_sysrq.py`, `src/kdive/mcp/worker_registration.py`,
`tests/jobs/test_diagnostic_sysrq.py`.

**Do:**
- `diagnostic_sysrq_handler(conn, job, *, resolver, secret_registry, artifact_store)`:
  1. **tx 1 (per-System advisory lock):** load System; verify live + local-libvirt + READY
     (else `CONFIGURATION_ERROR` `reason=system_changed_state`/appropriate); resolve
     `domain_name`; read `mark = len(read_console_log(path))`; commit.
  2. **lock-free:** `await asyncio.to_thread(control.diagnostic_sysrq, domain_name, trigger)`;
     run `capture_console_delta` with `read_console = lambda: asyncio.to_thread(read_console_log,
     path)`, `sleep = asyncio.sleep`.
  3. exit `no_output` → raise `CategorizedError(CONFIGURATION_ERROR, reason="no_console_output",
     remediation=<kernel.sysrq + PS/2 keyboard driver>)`.
  4. redact the raw slice via `Redactor(registry=secret_registry)`.
  5. **tx 2 (lock):** re-verify live+READY (else `system_changed_state`); `put_artifact`
     (`sysrq-diagnostic-<job_id>`, `owner_kind="systems"`, `tenant="local"`,
     `sensitivity=REDACTED`, `retention_class="console"`, `run_id=None`); register the row
     **insert-if-absent on object key** (mirror `console_rotate._existing_part_row`); audit
     `sysrq:{command}`; return `str(artifact.id)` (→ `result_ref`).
  - `artifact_store is None` → `CONFIGURATION_ERROR` (object storage not configured, with
    remediation) — the capture is the deliverable; do not silently no-op.
  - Record the capture-outcome metric (`captured`/`no_output`/`control_failure`) tagged by
    `provider_kind` (reuse the worker metrics pattern; a no-op meter under test).
- `worker_registration.py`: add `_register_diagnostic_sysrq_handler` passing `resolver`,
  `secret_registry`, and `object_stores.optional_upload_store`; append to `HANDLER_REGISTRARS`.

**TDD / acceptance (fake Control + scripted console reader + no-op sleep, real DB):**
- Happy path: artifact written (System-owned, REDACTED), `result_ref` == artifact id, audit
  row present; fake Control recorded the trigger.
- No-output → job `CONFIGURATION_ERROR` `no_console_output`.
- Registered secret in the delta is redacted in the stored object.
- Replayed handler run (same job) → no duplicate artifact row (insert-if-absent).
- Mid-capture state change (System set not-READY between tx1 and tx2) → `system_changed_state`.
- `artifact_store=None` → `CONFIGURATION_ERROR`.

## Task 6 — MCP tool `control.diagnostic_sysrq`

**Where:** the agent-facing admission path, in the `control.*` toolset next to `force_crash`.

**Files:** `src/kdive/mcp/tools/lifecycle/control.py`,
`tests/mcp/lifecycle/test_control_tools.py`.

**Do:**
- Handler `diagnostic_sysrq_system(pool, ctx, *, system_id, command, resolver, idempotency_key)`
  mirroring `force_crash_system` but: parse `command` via `SysRqCommand.parse` (unknown →
  `config_error` with allowed values / force_crash remediation); `require_role(ctx, project,
  Role.CONTRIBUTOR)` (no destructive gate); resolve provider kind via `resolver` and reject
  non-local-libvirt with `config_error` + remediation; require `SystemState.READY` (else
  `config_error` with `current_status`); enqueue `JobKind.DIAGNOSTIC_SYSRQ` with
  `SysRqPayload`, dedup key `{system_id}:diagnostic_sysrq:{command}:{idempotency_key or uuid4}`,
  wrapped in `keyed_mutation` (kind `"control.diagnostic_sysrq"`).
- `@app.tool(name="control.diagnostic_sysrq", annotations=_docmeta.mutating(),
  meta=_docmeta.maturity_meta(...))`. **Wrapper docstring + `Field` text** must state: it is
  non-destructive; the allowed `command` values; that it returns a `queued` job; that on
  success `refs.result` is the redacted console-dump artifact id to read with `artifacts.get`;
  and that no output → `configuration_error`. Choose the maturity marker consistent with
  recent additions gated on a live proof (see ADR-0276/0277/0278 precedent —
  `implemented` if the live proof runs in this PR, else the not-yet-proven marker).

**TDD / acceptance:**
- Allowed command → `queued` job with correct dedup key + payload (assert via DB).
- Unknown/destructive command → `configuration_error` (allowed values / force_crash hint).
- Non-local-libvirt System → `configuration_error`; not-READY → `configuration_error` with
  `current_status`; under-privileged ctx (VIEWER) → `authorization_denied`.
- Idempotency-key replay returns the prior envelope.

## Task 7 — Teardown reclaim of `sysrq-diagnostic-*`

**Where:** prevent a System-owned artifact leak past teardown.

**Files:** `src/kdive/jobs/handlers/systems.py`, `tests/jobs/test_systems_handler.py` (or the
existing teardown test).

**Do:** extend `_reclaim_console_artifacts` (or add a sibling reclaim invoked from
`teardown_handler`) to also delete objects+rows for the System whose `object_key LIKE
'%sysrq-diagnostic-%'`, mirroring the `_CONSOLE_PART_LIKE` pattern (objects before rows).

**TDD / acceptance:** a System with a `sysrq-diagnostic-*` artifact has the object deleted and
the row removed at teardown; a System without one is unaffected; existing console-part reclaim
still passes.

## Task 8 — Agent-facing surface guards + generated docs

**Where:** keep the drift guards green (adding a tool changes generated artifacts).

**Files:** `just docs` output (tool reference), `just rbac-matrix` output
(`docs/guide/safety-and-rbac.md`), `just resources-docs` output (agent-index / control
toolset purpose doc under `docs/guide/toolsets/`), and any registered-tool snapshot test
(`tests/mcp/core/test_app.py`).

**Do:** run the mutating regenerators (`just docs`, `just rbac-matrix`, `just resources-docs`),
review diffs, and update the control toolset purpose doc (#940/ADR-0284) to describe
`control.diagnostic_sysrq` by purpose. Update any tool-count/registry snapshot the addition
moves.

**TDD / acceptance:** `just docs-check`, `just rbac-matrix` (check mode), `just
resources-docs-check`, and the app/registry snapshot test all pass.

## Task 9 — `live_vm` proof (required by ADR-0285)

**Where:** the only test that falsifies the guest-keyboard/`kernel.sysrq` end-to-end mechanism.

**Files:** a `@pytest.mark.live_vm` test under `tests/` (mirror an existing live_vm test's
provision fixture), runnable via `just test-live` on this KVM host.

**Do:** provision a local-libvirt System on a built kernel + default catalog rootfs, call
`control.diagnostic_sysrq` with an allowlisted command, poll `jobs.wait`, and assert the job
succeeds with a non-empty redacted artifact readable via `artifacts.get`. Record the default
image's `kernel.sysrq` value; if unset, document the supported-kernel constraint in the
operator docs and the tool remediation. This proof is run before marking the tool
`implemented`; if it cannot pass on the default images, the PR body states the limitation and
the tool ships with the not-yet-proven maturity marker.

**TDD / acceptance:** the live proof passes on this host (`just test-live`), or the limitation
is documented and the maturity marker reflects it. The default (non-live) suite stays green
because `live_vm` is gated.

## Rollback / cleanup

Pure addition; revert removes the tool, handler, capture core, port method (+ stub), payload,
job kind, migration CHECK widening, teardown clause, and doc/guard updates. No data backfill.
Do not un-gate `live_vm`; do not weaken existing gates.
