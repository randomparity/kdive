# Force live-stack teardown design

## Scope

Issue #1733 requires a supported teardown path that ends kdive daemons which remain after the
existing SIGTERM grace period. Bring-up must retain its graceful-only behavior. The change also
replaces the surplus-worker manual `kill -9` remedy with the supported path and reports when the
initial SIGTERM could not be delivered.

[ADR-0527](../../adr/0527-scope-forced-daemon-termination-to-teardown.md) records the ownership
boundary.

## Alternatives

1. Escalate inside `stop_daemons`. This is compact but wrong because `up.sh` uses that helper and
   would kill a worker in a legitimate long-running job during ordinary bring-up.
2. Add `down.sh --force` and a separate force helper. This keeps default teardown and all bring-up
   calls graceful while giving operators one explicit destructive command. This is selected.
3. Make every teardown escalate automatically. This would make plain teardown destructive without
   an operator opt-in and would abandon jobs unexpectedly.

## Design

`stop_daemons` keeps sending SIGTERM and polling for ten seconds. It records and names pids for
which the signal command itself failed, separately from pids which were signalled but did not exit.
This diagnostic is process-local and is reset on each call.

`down.sh --force` first runs that unchanged graceful phase, then calls a teardown-only helper that
rescans remaining kdive daemons, sends SIGKILL with the same ownership-aware sudo selection, and
polls until none remain. Failure to signal or survivors after the force poll is a hard error, so
compose backends are not stopped while host daemons are still known to be running. Plain
`down.sh`, `up.sh`, and `restart_host_processes` never invoke the force helper.

`require_workers_alive` points surplus-worker recovery to `down.sh --force` and explains that the
command ends all matched kdive host processes and abandons in-flight jobs for lease-based reclaim.
When `stop_daemons` recorded unsignalled pids, the message identifies them so waiting is not offered
as a remedy for those processes.

## Error handling and safety

The local operator is trusted to invoke the script. `--force` is explicit and documented as
destructive to in-flight work. PID discovery remains constrained by the existing kdive daemon argv
matcher, and ownership-aware signalling avoids requiring sudo for caller-owned processes. A failed
SIGKILL is surfaced rather than swallowed.

## Threat model

### Boundary inventory and actors

- The local operator supplies CLI flags to `down.sh`; no remote or tenant-controlled entry point is
  added. The operator is trusted to administer the development host.
- Process-table data crosses from mutable host state into a privileged signal destination. Other
  local processes may exit or reuse a pid while teardown is running.
- `sudo` crosses from the invoking user to host-level process control when a matched daemon belongs
  to another uid.

### Controls

- Only the literal `--force` flag enables escalation; unknown arguments fail before signalling.
- Targets come only from the existing command-and-argv daemon matcher. Immediately before each
  SIGKILL, the helper rescans and requires the pid to remain in that matched set. Portable shell
  cannot make that check and signal atomic, so ADR-0527 records the remaining narrow reuse window.
- Ownership determines whether signalling uses `sudo`; signal-delivery failures name only numeric
  pids and fail before backend teardown. Polling is bounded to twenty half-second iterations.
- Error output contains process ids and lifecycle state but no environment, credentials, or daemon
  output.

### Out of scope

This local operator command does not defend against a malicious local account deliberately shaping
process argv to match kdive. Host account isolation owns that threat. It also does not introduce a
second pidfd-based process-control implementation solely to close the portable-shell reuse residual.

## Tests

Shell-script tests stub daemon discovery, sleep, kill, sudo, and Docker to prove:

- plain stop and bring-up paths never send SIGKILL;
- `down.sh --force` escalates only after the graceful helper and stops backends only on success;
- failed signal delivery is named separately from a signalled survivor;
- surplus-worker guidance names `down.sh --force` rather than manual `kill -9`;
- usage and runbook text describe the destructive and job-reclaim consequences.
