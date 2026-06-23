# Guide the early-boot console-crash case in postmortem / vmcore.fetch (#734, D4)

- **Status:** Draft
- **Date:** 2026-06-23
- **ADR:** [0227](../adr/0227-early-boot-console-crash-postmortem-guidance.md)
- **Issue:** #734 (part of #736; coordinates with #735)

## Problem

For an early-boot panic the kernel crashes **before** kdump's capture kernel is
loaded via kexec, so kdump can never produce a vmcore. The operator declares this at
create time as `expected_boot_failure = console_crash`, and the **console artifact**
is the evidence source. Two MCP surfaces report bare states that dead-end an agent
instead of redirecting it to the console:

1. **`postmortem.triage` / `postmortem.crash`** resolve the Run's captured vmcore via
   `resolve_run_vmcore_target` (`mcp/tools/_vmcore_targets.py`). When no core exists
   it raises a `not_found` carrying `data.reason = "no_vmcore"`. The `not_found`
   `detail` is suppressed to `"not found"` by the ADR-0123 no-leak seam. The envelope
   never says that for a `console_crash` run *no vmcore is expected by design* and the
   console is where to look — even though the Run (including `expected_boot_failure`)
   is loaded right at the `no_vmcore` raise.

2. **`vmcore.fetch` on a non-`CRASHED` System** returns
   `_config_error(system_id, data={"current_status": <state>})` with a **null**
   `detail` (`mcp/tools/lifecycle/vmcore.py`). It names the state token but not why
   that blocks capture.

## Constraint: the no-leak seam (ADR-0123)

`ToolResponse.failure` runs `detail` through `suppressed_detail(category, detail)`,
keyed purely on category: a `not_found` `detail` is always overwritten with the fixed
`"not found"` constant. So a real narrative `detail` is **impossible** on a
`not_found` envelope. `configuration_error` is **not** suppressed — its `detail`
passes through. This is why item 1 below reclassifies the console-crash case (see the
ADR; it mirrors the ADR-0142 `debug.start_session` expected-crash redirect, which is
`configuration_error` for the same reason).

By the time `resolve_run_vmcore_target` raises `no_vmcore`, the Run has already
resolved (project-scoped via `ctx.projects`, viewer role enforced), so this is **not**
the absent-Run / ungranted-project no-leak path (that miss carries no reason token at
all). A console-crash redirect on a resolved, viewer-authorized run is not a
membership leak; the narrative is author-controlled static text identical for every
`console_crash` run.

## Design

All surfaced text is **author-controlled** — fixed templates, no guest output,
exception message, secret, hostname, object-store key, or caller-un-supplied resource
name interpolated. One shared narrative constant (`mcp/tools/lifecycle/vmcore.py`) so
the wording cannot drift.

### 1. Postmortem console-crash redirect

The console-crash kind is propagated **on the `NO_VMCORE` error**, not re-fetched in
the handler. This is the single load-bearing mechanism: the resolver already holds the
`run` object at the `NO_VMCORE` raise site, so re-fetching in the handler would
duplicate the resolver's `RUNS.get` + project-scope check, force the handler to
re-parse `run_id` (the handler holds only the string; `uid` is resolver-internal), and
silently desync if the resolver's precondition order ever changes.

**Resolver (`mcp/tools/_vmcore_targets.py`).** At the `vmcore_ref is None` branch,
`resolve_run_vmcore_target` raises the `NO_VMCORE` `not_found`. The
`expected_boot_failure` key is attached to `details` **only when the run's declared
kind is exactly `"console_crash"`** — not for any other kind and not for a run that
declared none:
`details={"reason": NO_VMCORE, "expected_boot_failure": "console_crash"}` for a
console-crash run, and `details={"reason": NO_VMCORE}` (byte-identical to today) for
every other no-vmcore run. The kind is read from `run.expected_boot_failure` (the
serialized dict's `kind` key — an author-controlled enum-like token, the same value
`runs.get` already surfaces, never guest/exception text).

Scoping the key to `console_crash` (rather than always attaching `<kind-or-None>`)
is load-bearing for the fall-through contract: the non-console-crash path falls
through to `vmcore_target_failure` → `failure_from_error` → `safe_error_details`,
which forwards **every** JSON scalar in `details` to the envelope `data`. Attaching an
unconditional kind would surface a new `data.expected_boot_failure` key on the
non-console-crash `no_vmcore` `not_found` envelope the day a second
`expected_boot_failure.kind` is introduced (today the domain Literal admits only
`console_crash`, but the contract must not depend on that staying true). Conditional
attachment keeps that envelope's `data` exactly `{reason: no_vmcore}` regardless of
future kinds. A new `_precondition` helper variant takes the optional kind so only
the console-crash `NO_VMCORE` raise carries it.

The other preconditions (`NO_DEBUGINFO`, `NO_BUILD`) and the absent-Run miss are
unchanged. The project-scope and viewer-role checks are already complete before
`NO_VMCORE` is raised (lines 63-66 of the resolver), so anything downstream performs
**no further authz**.

**Handler (`mcp/tools/lifecycle/vmcore.py`).** `_postmortem_crash` catches the
resolver `CategorizedError` and currently maps it via `vmcore_target_failure(run_id,
exc)`. Extend that catch so that **only** when
`exc.details.get("reason") == NO_VMCORE` **and** `exc.details.get(
"expected_boot_failure") == "console_crash"` it returns the console-crash redirect
instead; every other case (other reasons, absent reason, a missing or
non-`console_crash` kind) falls through to the unchanged `vmcore_target_failure`
path. The redirect returns `config_error(run_id, detail=<narrative>, data={...})`
with:
  - `data.reason = EXPECTED_CONSOLE_CRASH` (`"expected_console_crash"`),
  - `data.expected_boot_failure = "console_crash"`,
  - `detail` = the shared narrative constant explaining the early-boot-crash-before-
    kexec case and that the console artifact (via `runs.get`) is the evidence source,
  - `suggested_next_actions = ["runs.get", "artifacts.list"]`.

`postmortem.triage` delegates to `postmortem.crash`, so the redirect surfaces on both;
the existing `if resp.status == "error": return resp` short-circuit in
`_postmortem_triage` passes the `configuration_error` straight through (it does **not**
relabel actions, which is correct — the redirect's own next actions must win).

Because the redirect is reached only through the *caught* `CategorizedError`, a
non-viewer (whom the resolver rejects with `AuthorizationError`, a plain `Exception`,
**before** any precondition check) never reaches it — authz is never weakened.

### 2. `vmcore.fetch` non-`CRASHED` detail (`mcp/tools/lifecycle/vmcore.py`)

`_fetch_vmcore`'s `if system.state is not SystemState.CRASHED:` branch gains a
fixed-template `detail`:

> "system must be in CRASHED state to capture a vmcore; current state = `<state>`"

`<state>` is `system.state.value` — the System's own `SystemState` enum token,
already surfaced in `data.current_status`, so echoing it leaks nothing (ADR-0123).
`data.current_status` is unchanged; the category stays `configuration_error`.

## Acceptance

- A `console_crash` run that resolves to no vmcore: `postmortem.triage` (and
  `postmortem.crash`) returns `configuration_error` with `data.reason ==
  "expected_console_crash"`, `data.expected_boot_failure == "console_crash"`, and
  `suggested_next_actions == ["runs.get", "artifacts.list"]`. The `detail` is exactly
  the shared narrative constant (the test asserts `detail == <constant>` against the
  named module symbol, not merely non-null, so a vacuous detail fails); the constant
  contains the stable substrings `"kexec"` and `"console"` so its meaning is pinned.
- A run with **no** `expected_boot_failure`, or one whose kind is not `console_crash`,
  that resolves to no vmcore: unchanged — `not_found` + `data.reason == "no_vmcore"` +
  `suggested_next_actions == ["vmcore.fetch", "runs.get"]`. The test asserts the
  envelope `data` carries **no** `expected_boot_failure` key (pinning the conditional
  attachment so a future second kind cannot silently leak it through
  `safe_error_details`).
- A `no_debuginfo` / `no_build` / absent-Run miss: unchanged, regardless of
  `expected_boot_failure` (the redirect is scoped to `no_vmcore`).
- `vmcore.fetch` on a non-`CRASHED` System: `configuration_error` with a non-null
  `detail` naming the required CRASHED state and the current state, and
  `data.current_status == <state>` as before.
- A non-viewer caller still raises `AuthorizationError` (the redirect never weakens
  authz).
- All `suggested_next_actions` are literal valid registered tool names.

## Out of scope

- `#735`'s `refs.console` on `runs.get` (parallel issue; this points the agent at
  `runs.get` conceptually and does not depend on it landing first).
- Detecting the case in the shared `_vmcore_targets.py` resolver (other vmcore-centric
  callers — `introspect.*` — do not want the console redirect; see the ADR).
- Any schema, migration, port, or DB change (none required).
