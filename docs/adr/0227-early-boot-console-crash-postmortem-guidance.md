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
   envelope **instead of** the suppressed `not_found`:
   - category stays `not_found` (no core *was* found — the category is honest), but
   - `data.reason` becomes `expected_console_crash` (a new, distinct reason token, so
     a client can branch on it),
   - `data.expected_boot_failure = "console_crash"` echoes the run's declared kind,
   - `suggested_next_actions = ["runs.get", "artifacts.list"]` point at the tools
     that surface the console artifact reference (`runs.get` carries `refs.console`
     per #735; `artifacts.list` enumerates the run's artifacts), and
   - a real, non-suppressed narrative `detail` explains the early-boot-crash-before-
     kexec case and that the console is the evidence source.

   The narrative is carried as `data.guidance` (always passes the seam) *and* as the
   `detail` field. Because the tailored envelope is built with a non-suppressed
   construction path for this one case, the `detail` reaches the client. A run with
   no `expected_boot_failure`, or one whose kind is not `console_crash`, keeps the
   existing reason-keyed `no_vmcore` envelope unchanged.

2. **Non-null `detail` on `vmcore.fetch` for a non-`CRASHED` System.** The
   `current_status` `_config_error` gains a fixed-template `detail`: "system must be
   in CRASHED state to capture a vmcore; current state = `<state>`". The state token
   is the System's own `SystemState` enum value (already surfaced in `data`), never a
   secret, hostname, or caller-un-supplied name, so it is safe to echo (ADR-0123).

3. **One shared narrative constant.** The console-crash guidance string is a single
   module constant so the wording cannot drift, mirroring the ADR-0223 shared-
   `remediation` pattern.

## Consequences

- An agent that triages an early-boot console-crash run now gets a `not_found` whose
  `detail` and `data.guidance` explain that no vmcore is expected by design and point
  at `runs.get` for the console reference — instead of a bare `"not found"` /
  `no_vmcore`. The category stays `not_found` (no core exists), preserving the
  is-error contract; only the reason token, next actions, and narrative change for
  this one case.
- A new `data.reason` value `expected_console_crash` joins the `no_vmcore` /
  `no_debuginfo` / `no_build` vocabulary. It is additive; existing clients keying on
  the old tokens are unaffected, and a non-console-crash run still reports
  `no_vmcore`.
- `vmcore.fetch` on a non-crashed System now carries a one-line reason. The
  `current_status` data field and the `configuration_error` category are unchanged,
  so existing callers see only a populated `detail` where it was null.
- No port, schema, migration, or dependency change. The change is confined to two MCP
  read/admission handlers and reads only fields already on the Run/System rows.

## Alternatives considered

- **Put the narrative in the suppressed `not_found` `detail`.** Rejected: ADR-0123
  forces a `not_found` `detail` to the `"not found"` constant, so the narrative would
  never reach the client. The tailored-envelope path exists precisely because the
  console-crash case is a resolved-and-authorized run, not a no-leak lookup miss.
- **Change the `no_vmcore` category to `configuration_error`** so its `detail` passes
  the seam unconditionally. Rejected: the postmortem genuinely found no core, which
  is `not_found` per ADR-0097/0142; flipping the category for narrative convenience
  would mis-describe the failure and break clients that branch on `not_found` for the
  capture-not-ready signal. Keeping `not_found` and adding a distinct reason token is
  the honest split.
- **Surface the redirect only in `data`, leave `detail` suppressed.** Rejected: the
  acceptance criterion asks for a `detail` an agent reads without parsing `data`, and
  a resolved-authorized run's console-crash status is not leak-sensitive — there is no
  reason to withhold it from `detail`.
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
