# ADR 0227 — Guide the early-boot console-crash case in postmortem/vmcore surfaces

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0142](0142-diagnostic-precondition-ergonomics.md) (the shared
  Run → vmcore target resolver and its reason-keyed failure envelope this reads),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (the no-leak `detail` suppression
  seam that forces a `not_found` `detail` to a fixed constant),
  [ADR-0064](0064-expected-boot-failures-artifact-search.md) (the
  `expected_boot_failure` Run metadata this branches on).

## Context

Black-box review (`BLACK_BOX_REVIEW.md`, defect **D4**, #734) found that the
postmortem and capture surfaces dead-end an agent for the *early-boot console-crash*
case instead of redirecting it.

An early-boot panic crashes the kernel **before** kdump's capture kernel is loaded
via kexec, so kdump can never produce a vmcore. For these runs the operator declares
`expected_boot_failure = console_crash` at create time, and the **console artifact**
— not a vmcore — is the evidence source. Today the surfaces report bare states with
no narrative:

1. **`postmortem.triage` / `postmortem.crash`.** Both resolve the Run's captured
   vmcore through `resolve_run_vmcore_target` (`mcp/tools/_vmcore_targets.py`), which
   raises a `not_found` carrying `data.reason = "no_vmcore"` when no core exists. The
   `run` object — including `expected_boot_failure` — is loaded right there, so the
   redirect information is in scope but unused. The envelope tells the agent "no
   vmcore" but not that for this run *none is expected by design* and the console is
   where to look. `not_found`'s `detail` is suppressed to the fixed `"not found"`
   constant by the ADR-0123 no-leak seam, so the narrative cannot live on that
   envelope.

2. **`vmcore.fetch` on a non-`CRASHED` System.** Returns
   `_config_error(system_id, data={"current_status": …})` with a **null `detail`**
   (`mcp/tools/lifecycle/vmcore.py`). It states the state token but never says *why*
   that blocks capture.

`configuration_error` is **not** a suppressed category (ADR-0123), so a `detail` on
the `vmcore.fetch` path passes through to the client unchanged. The console-crash
narrative is author-controlled static text describing the *expected* design
behavior; it is identical for every `console_crash` run and names no resource the
viewer-authorized caller did not already resolve, so it carries no resource-existence
signal.

The constraint bounding the fix: by the time the resolver raises `no_vmcore` the Run
has already resolved (project-scoped, viewer role enforced), so this is **not** the
absent-Run / ungranted-project no-leak path (that miss carries no reason token at
all). Surfacing a console-crash redirect on a resolved-and-authorized run is not a
membership leak.

This coordinates with #735 (which adds `refs.console` to `runs.get`). The narrative
points the agent at `runs.get` to obtain the console artifact reference; it does not
depend on #735 landing first and works against `main` as-is.

## Decision

1. **Console-crash redirect on the postmortem `no_vmcore` miss.** In the postmortem
   handler (`mcp/tools/lifecycle/vmcore.py`), when `resolve_run_vmcore_target` raises
   a `no_vmcore` `not_found`, consult the already-loaded Run's
   `expected_boot_failure`. If its `kind == "console_crash"`, return a tailored
   envelope **instead of** the suppressed `not_found`, categorized
   `configuration_error`:
   - The category is `configuration_error`, **not** `not_found`. This is the
     deliberate, established way (ADR-0142's `debug.start_session` "booted into
     expected crash" redirect) to surface an author-controlled redirect `detail`: the
     ADR-0123 no-leak seam force-suppresses a `not_found` `detail` to the `"not
     found"` constant, so a real narrative `detail` is *impossible* on a `not_found`
     envelope. For a `console_crash` run the failure is not "the core is not yet
     captured" (a transient `not_found`) but "this run can never produce a core — you
     called the wrong tool for its declared failure mode", which is a caller/usage
     mismatch — `configuration_error` — exactly as ADR-0142 classifies the analogous
     live-attach-on-expected-crash redirect.
   - `data.reason = "expected_console_crash"` (a new, distinct reason token a client
     can branch on, paired with the prose detail per the ADR-0174 reason+detail
     pattern).
   - `data.expected_boot_failure = "console_crash"` echoes the run's declared kind.
   - `suggested_next_actions = ["runs.get", "artifacts.list"]` point at the tools that
     surface the console artifact reference (`runs.get` carries `refs.console` per
     #735; `artifacts.list` enumerates the run's artifacts).
   - A real, non-suppressed narrative `detail` explains the early-boot-crash-before-
     kexec case and that the console is the evidence source. It passes the seam
     because `configuration_error` is not a suppressed category, and it leaks nothing:
     the run already resolved (project-scoped, viewer-authorized) and the text is
     identical for every `console_crash` run.

   A run with no `expected_boot_failure`, or one whose kind is not `console_crash`,
   keeps the existing reason-keyed `no_vmcore` `not_found` envelope unchanged.

2. **Non-null `detail` on `vmcore.fetch` for a non-`CRASHED` System.** The
   `current_status` `_config_error` gains a fixed-template `detail`: "system must be
   in CRASHED state to capture a vmcore; current state = `<state>`". The state token
   is the System's own `SystemState` enum value (already surfaced in `data`), never a
   secret, hostname, or caller-un-supplied name, so it is safe to echo (ADR-0123).

3. **One shared narrative constant.** The console-crash guidance string is a single
   module constant so the wording cannot drift, mirroring the ADR-0223 shared-
   `remediation` pattern.

## Consequences

- An agent that triages an early-boot console-crash run now gets a
  `configuration_error` whose `detail` explains that no vmcore is expected by design
  and whose `suggested_next_actions` point at `runs.get` for the console reference —
  instead of a bare `not_found` / `"not found"` / `no_vmcore`.
- The error **category changes** for this one case: a `console_crash` run that
  resolves to no core now reports `configuration_error` (was `not_found`). A client
  that branched on `not_found` + `data.reason == "no_vmcore"` for the console-crash
  run will now see `configuration_error` + `data.reason == "expected_console_crash"`.
  This is the same trade ADR-0142 made for the `debug.start_session` expected-crash
  redirect; the category shift is what makes the narrative `detail` deliverable. A
  non-console-crash run (a run that simply has not captured yet) is **unchanged** —
  still `not_found` + `no_vmcore`.
- A new `data.reason` value `expected_console_crash` joins the `no_vmcore` /
  `no_debuginfo` / `no_build` vocabulary. It is additive; existing clients keying on
  the old tokens for non-console-crash runs are unaffected.
- `vmcore.fetch` on a non-crashed System now carries a one-line reason. The
  `current_status` data field and the `configuration_error` category are unchanged,
  so existing callers see only a populated `detail` where it was null.
- No port, schema, migration, or dependency change. The change is confined to two MCP
  read/admission handlers and reads only fields already on the Run/System rows.

## Alternatives considered

- **Keep `not_found` and put the narrative in the suppressed `detail`.** Rejected:
  ADR-0123 forces a `not_found` `detail` to the `"not found"` constant inside
  `ToolResponse.failure`, keyed purely on category, so the narrative would never reach
  the client. Delivering a real `detail` requires a non-suppressed category, which is
  why the console-crash case is reclassified `configuration_error`.
- **Keep `not_found` and surface the redirect only in `data`** (`data.guidance` /
  `data.reason`), leaving `detail` suppressed. This is the ADR-0142 shape for the
  vmcore preconditions and is leak-safe. Rejected as the chosen path because the #734
  acceptance asks specifically for a `detail` an agent reads without parsing `data`,
  and a resolved-authorized run's console-crash status is not leak-sensitive — there
  is no reason to withhold it from `detail`. ADR-0142 itself routes the directly
  analogous `debug.start_session` expected-crash redirect through
  `configuration_error` for exactly this reason, so this ADR follows that precedent
  rather than the `data`-only precondition shape.
- **Reclassify *all* `no_vmcore` misses as `configuration_error`.** Rejected: a run
  that simply has not captured a core yet (no `expected_boot_failure`) is genuinely
  `not_found` per ADR-0097/0142 and is retry-able once `vmcore.fetch` runs; only the
  `console_crash` subset is a permanent design fact warranting the
  caller-mismatch category. Scoping the reclassification to `console_crash` keeps the
  honest split.
- **Detect the console-crash case in the resolver** (`_vmcore_targets.py`) rather
  than the handler. Rejected: the resolver is shared by `introspect.*` and other
  vmcore-centric callers (ADR-0165) for which the postmortem-specific console
  redirect is not the right next action; keeping the redirect in the postmortem
  handler keeps the shared resolver's contract (reason-keyed `not_found`) intact and
  scopes the new next-action set to the surfaces #734 names.
- **Branch `suggested_next_actions` on whether #735 has landed.** Rejected as a
  speculative coupling: `runs.get` is the correct redirect regardless of whether it
  yet carries `refs.console`; once #735 lands the same action resolves the console
  reference with no change here.
