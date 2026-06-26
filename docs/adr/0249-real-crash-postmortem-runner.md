# ADR 0249 — Wire a real `crash(8)` runner into the Retrieve postmortem

- **Status:** Accepted
- **Date:** 2026-06-25
- **Issue:** [#816](https://github.com/randomparity/kdive/issues/816)
- **Spec:** [`../superpowers/specs/2026-06-25-real-crash-runner-816.md`](../superpowers/specs/2026-06-25-real-crash-runner-816.md)
- **Depends on:** [ADR-0084](0084-remote-control-two-phase-vmcore-retrieve.md) (the provider-neutral
  worker-side crash postmortem this completes), [ADR-0175](0175-partial-tool-maturity-reason.md)
  (the maturity/promotion contract the two tools carry).

## Context

The Retrieve plane's crash postmortem (`postmortem.crash`, `postmortem.triage`) returns
`missing_dependency` over the deployed HTTP server even with a real captured vmcore present.
The production provider assembly wires the crash subprocess seam to a no-op stub:
`local_libvirt/retrieve.py` builds `from_env` with `run_crash=default_run_crash`, and
`remote_libvirt/retrieve/facade.py` defaults `run_crash = default_run_crash`. That stub
(`debug_common/crash_postmortem.py`) raises `MISSING_DEPENDENCY` unconditionally.

Every other host-bound Retrieve seam — `_real_wait_for_vmcore`, `_real_read_build_id`,
`_real_host_dump_capture` (local); the kdump/host-dump capturers (remote) — ships a real
`# pragma: no cover - live_vm` implementation that production wires. The crash seam is the
only one whose real counterpart was never written, so the postmortem feature is inert in a
deployed server, and the two tools' `partial` promotion bar ("a recorded live_stack run
runs crash commands over a real captured core") is unsatisfiable: the live_stack server
wires the stub.

A second latent defect: `CrashResult.exit_status` is never read. `run_crash_postmortem`
uses only `crash.stdout`, so a `crash(8)` run that exits non-zero (e.g. an incompatible
core it cannot open) would be reported as a successful postmortem with an empty transcript.

## Decision

Wire a real `crash(8)` subprocess runner into both providers' production assembly, and make
the exit-status check load-bearing in the shared helper.

1. **Add `_real_run_crash`** to `debug_common/crash_postmortem.py`, replacing
   `default_run_crash` as the production default (the stub is deleted, not deprecated). It
   resolves the `crash` binary via an injected `crash_path_finder` (`shutil.which` by
   default); a missing binary raises `MISSING_DEPENDENCY` naming the missing utility. It
   runs a fixed argv `crash -s <vmlinux> <vmcore>` with the validated command batch on
   **stdin only** (never argv), bounded by `_CRASH_TIMEOUT_S` (300 s). A
   `subprocess.TimeoutExpired` or post-`which` `OSError` maps to `INFRASTRUCTURE_FAILURE`;
   otherwise it returns `CrashResult(exit_status, stdout, stderr)`. The `subprocess.run`
   itself is a thin `# pragma: no cover - live_vm` helper; the argv construction and the
   binary-absent branch are unit-tested off the gate (the injected path-finder), mirroring
   `PygdbmiController.gdb_path_finder`.

2. **Guard the exit status in `run_crash_postmortem`.** A non-zero `crash.exit_status`
   **with an empty/whitespace transcript** (the init-failure shape) raises
   `INFRASTRUCTURE_FAILURE` carrying `exit_status` and the **redacted, capped** stderr. The
   guard is conservative — `crash(8)` continues a batch past per-command errors, so a
   non-zero exit that still produced a transcript returns it rather than discarding it. The
   check lives in the provider-neutral helper so both providers benefit and it is
   unit-testable without `/usr/bin/crash`. The runner sets `cwd` to a worker-owned temp dir
   so crash never needs a writable process CWD.

3. **Wire it.** `LocalLibvirtRetrieve.from_env` and `RemoteLibvirtRetrieve.__init__` pass
   `run_crash=_real_run_crash`.

4. **Maturity.** With the stub gone, a live run over a real core can exercise the real path.
   On a passing live proof, `postmortem.crash`/`triage` promote to `implemented`
   (`maturity_detail` removed, the `tests/mcp/core/test_tool_docs.py` guard updated). Absent
   a completed proof they stay `partial` with corrected, now-satisfiable text.

No tool surface, parameter, RBAC, schema, env var, or persistence change; no migration.

## Consequences

- The deployed worker runs the real `crash(8)` over captured cores when the binary is
  present; absence is reported honestly as `missing_dependency` naming the binary.
- `crash(8)` failures (non-zero exit, timeout) surface as typed `infrastructure_failure`
  with redacted stderr instead of an empty "successful" transcript.
- The real path gains an executable proof (a `live_vm` test driving `/usr/bin/crash`), and
  the worker host now needs `crash(8)` installed for postmortem to function — a host
  prerequisite alongside `drgn`/`libguestfs`.
- `default_run_crash` is removed; remote's `default_read_vmcore_build_id` stays (a separate
  remote-only gap, out of scope).

## Considered & rejected

- **Option 2: declare the tools `live_vm`-only, keep the production stub.** The deployed
  MCP server is the product; a postmortem usable only inside pytest is a phantom feature.
  The crash subprocess runs on the worker exactly like `drgn`/`gdb`/`libguestfs`, which all
  run for real in production.
- **Keep the exit-status check inside the real runner only.** Putting it in the shared
  helper makes it provider-neutral, unit-testable without the binary, and makes the
  existing `CrashResult.exit_status` field load-bearing.
- **Feed the batch via `crash -i <cmdfile>` or argv.** Stdin keeps the argv fixed (no
  per-command argv-injection surface) and matches `_exec_live_script`; the upstream
  validator already sanitizes the batch.
- **Also fix remote's `default_read_vmcore_build_id`.** Deferred as a separate remote-only
  gap.
