# Remote-libvirt teardown of a preparing activation without PREP evidence (#3016)

Issue: [#3016](https://github.com/randomparity/kdive/issues/3016). Decision record: the
2026-10-01 amendment to [ADR-0620](../../adr/0620-authority-owned-system-teardown.md).
Builds on #2961 (teardown of a `preparing` activation) and #3017 (routing a never-authorized
`preparing` activation; [spec](2026-09-30-unrouted-preparing-teardown-3017-design.md)).

## Problem

`runs.boot` creates the activation `preparing` with a pending reservation
(`mcp/tools/lifecycle/runs/steps.py:568-590`). The remote module attempt opens its reap
obligation only during preparation (`prepare_remote_module_on_authority_host`). A remote-libvirt
activation that stops before that has no row for which `read_reap_preparation`
(`db/remote_module_attempt_obligations.py:251-273`) returns a receipt. `build_external_boot_payload`
(`jobs/handlers/external_boot/admission.py:116-125`) then refuses every remote-libvirt
operation in `_REMOTE_MODULE_LIFECYCLE_OPERATIONS`, `teardown` included, with "remote module
lifecycle has no retained PREP evidence". `systems.teardown` returns
`external_boot_teardown_authority_unresolved` and the System has no supported exit.

## What the teardown does with the receipt

Read against `b3316923c`:

1. `teardown_handler` (`jobs/handlers/external_boot/lifecycle.py:1060-1149`) passes
   `before_port=complete`. `complete` returns an `ExternalBootDerivedTeardownCompletion` or
   raises on every path. `run_operation` (`runner.py:739-744`) returns a non-`None` `before_port`
   result before it calls `call_port`. So `_execute`, and with it
   `_execute_remote_module_lifecycle` (`lifecycle.py:99-146`, the only reader of the payload's
   `remote_module_attempt_v1` in the worker), never runs for purpose `teardown`. The
   `AuthorityOperation.TEARDOWN` member of its operation set is unreachable.
2. The authority host owns module reaping at System teardown.
   `RemoteExternalBootAuthorityAdapter.execute_system_teardown`
   (`providers/remote_libvirt/external_boot_authority.py:2129-2222`) reads its own durable
   provider-private state. With a terminal module record it reaps through the module host. With
   an unfinished preparation record whose absence it cannot prove, it returns quarantine facts,
   so the 0147 receipt yields `retained_quarantine` and requeues the job. With neither record it
   destroys, undefines and removes, with no module call. The existing test
   `test_remote_system_teardown_restarts_after_lost_storage_response` covers that last path.
3. `finalize_external_boot_authority_teardown` (0147) does not read the payload's receipt.

So the receipt is not part of the teardown's reap contract. A module mutation that happened
without an open reap obligation is still reaped or quarantined by the authority host. Admitting
the teardown without a receipt removes no reap that the teardown performed.

## Design

In `build_external_boot_payload`, keep the read and the ambiguity refusal. Skip the
missing-receipt refusal only when the purpose is `teardown` and the activation is `preparing`.
The payload then carries `remote_module_attempt_v1: null`, which `TeardownPayload` already
accepts. When a receipt exists, it is still carried unchanged.

Not changed:

- `lifecycle.py`. The teardown path never reaches the receipt check at `lifecycle.py:123-125`
  (point 1 above), so the refusal is removed, not moved.
- The authority host, the 0147 receipt, and the schema. No migration.
- Every other remote module lifecycle operation (`recover`, `resolve-conflict`, `release`,
  `cleanup`), and teardown of a non-`preparing` activation. Both still refuse without a
  receipt (operator exclusion).

### Alternatives

- **Drop the receipt requirement for every teardown.** Point 1 shows that this is sound, but
  the operator excluded it from this issue.
- **Synthesize a receipt or open a reap obligation at admission.** This writes durable module
  state for an attempt that never ran, and nothing on the teardown path reads it.
- **Do nothing.** The System keeps no exit through `systems.teardown`. That is the reported
  defect.

## Failure model

1. **Actors and deployments**
   - A project admin calling `systems.teardown`. Server plus a worker plus a remote-libvirt
     authority host, as in the operator lab deployment.
2. **Invariants and assets at stake**
   - Remote module volumes created by a preparation must be reaped or quarantined, never
     orphaned.
   - The pending reservation ends uncredited exactly once. A ready reservation credits exactly
     once (0147 receipt).
   - Every remote lifecycle operation except `preparing` teardown still requires a receipt.
3. **Accepted failure classes**
   - A `preparing` activation with a mutation obligation but no reap obligation (preparation
     interrupted mid-flight) is admitted without a receipt. This is accepted because the
     authority host quarantines its unfinished preparation (point 2), and the 0147 receipt
     requeues the job.
   - An activation that leaves `preparing` between admission and the worker claim is torn
     down with a payload that has no receipt. This is accepted because the teardown handler
     never reads the receipt (point 1).
4. **Covered elsewhere**
   - An activation with no authority row: #3017.
   - The allocation-release fence: #2992.
   - Other remote module lifecycle operations: the operator.

## Success

- `build_external_boot_payload` returns a `TEARDOWN` payload whose `remote_module_attempt_v1`
  is `None` for a remote-libvirt `preparing` activation with no retained receipt. With one
  retained receipt it carries that receipt. With two it refuses as ambiguous.
- For a remote-libvirt activation with no receipt, it still refuses `teardown` of a
  `recovery_failed` activation and `release` of a `recovered` activation.
- The remote-libvirt teardown handler, given that payload and a module capability that fails
  if called, ends with the activation and System `torn_down`. A pending reservation ends with
  no reservation row and no release row. A ready reservation ends with exactly one release row.
- `systems.teardown` on a remote-libvirt System whose newest activation is `preparing` with no
  receipt returns `queued`. The authority-marked teardown job has no `remote_module_attempt_v1`.

## Validation

- Admission tests in `tests/jobs/handlers/external_boot/test_admission.py`, against the
  migrated database.
- Handler test in `tests/jobs/handlers/external_boot/test_prepared_before_admission.py`, with
  the remote runtime. The module lifecycle entry point is patched to fail if reached.
- MCP test in `tests/mcp/lifecycle/test_systems_tools.py`.
- `just lint`, `just type`, and `just records`. `just ci` runs at push.
- Remote-libvirt live tier, if a lab host can run it. Otherwise the PR states that only the
  DB-backed and handler arms ran.
