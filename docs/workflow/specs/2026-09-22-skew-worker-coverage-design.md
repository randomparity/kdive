# Live-stack skew worker coverage

## Problem and scope

Issue #2652 and debt 0002 show a false-fresh preflight: worker 2 can run a
different commit while ADR-0482's fixed URL map probes only worker 1. The
preflight remains the owner of coverage accounting; its direct caller's cache
must recheck the fleet before reuse. The launcher and lifecycle remain unchanged.
[ADR-0674](../../adr/0674-count-live-workers-for-skew-preflight.md)
chooses the host process table for deployed worker inventory.

The operator approved these exclusions on 2026-09-22: worker lifecycle and
health endpoint redesign (lifecycle/health owners), unrelated verdict policy
changes (skew policy owner), and other debt records (their owners).

## Global constraints

Python 3.14 with uv; Linux live-stack hosts on x86_64 and ppc64le. No new
dependency, endpoint, launch behavior, or verdict kind. Preserve worker 1's
default health port and ADR-0482's policy table. The default suite uses injected
inventory; a live proof uses the provisioned host's actual process table.

## Design

`skew.py` identifies PIDs of commands whose process name is a Python launcher
and whose arguments end in the exact `-m kdive worker` invocation. A fixed-argv
`ps` call uses `SubprocessCommandRunner` and `MonotonicDeadline` from
`kdive.processes.lifecycle.systemd.systemd_worker_runtime`. That existing runner
terminates the child on a three-second deadline or 256-KiB output cap. Failure,
malformed output, or more than eight workers makes inventory unavailable. The
caller can inject an inventory function for deterministic tests.

`probe_stack_skew` retains the URL map and grading. It samples worker PIDs
before and after the HTTP probes and counts worker URLs that returned a
`version` object. If either inventory is unavailable or empty, the PID sets
change, or the final count differs from returned worker builds, it appends one
actionable `worker-inventory: unknown`. It returns a `SkewProbe` holding both
results and the final validated PID set; uncertainty stores no reusable PID set.
`conftest.py` caches that exact returned set with the results, reads inventory
before each reuse, and probes again if the set changed or cannot be read. A
worker starting after a probe cannot be attached to the earlier fresh result;
the next `require_stack` call sees the different set and probes again.
An explicit shared health bind has no individually identified worker URL and
therefore cannot establish worker coverage. The verdict policy is unchanged.
The obsolete assumption that probing worker 1 proves worker coverage is
removed from the runbook and debt status.

## Success and validation

- One real or injected worker and one returned worker build keep the existing
  verdict and no inventory result; worker 1 stays on 9465.
- Two observed workers with only worker 1 probed append `unknown`, including
  when every returned build is at HEAD. An unreadable inventory does likewise.
- A worker starting during the probes yields `unknown`; a worker starting after
  a cached fresh result triggers a new probe on the next `require_stack` call.
- Zero or more than eight workers, malformed or oversized `ps` output, and an
  explicit shared health bind cannot produce an all-`fresh` result.
- Focused pytest covers PID parsing, injection, and a worker start between probe
  completion and cache insertion; the actual `ps` runner gets
  a host read smoke test. Run the two-worker live proof on a provisioned host if
  available, report which arm ran, and mark debt 0002 resolved.

## Failure model

- Actors and deployments: the operator runs the live-stack tier on the same
  Linux host as its app processes; another local user may run unrelated Python.
- Invariants and assets: a fresh-only result requires stable, equal worker PID
  sets around probes and an equal count of returned worker builds; cached results
  require an equal current PID set.
- Accepted failure classes: a worker starting after the final inventory read
  but before a test action is seen on the next `require_stack` call, because
  the cached key remains the earlier validated set. ADR-0574's operator
  serializes stack bring-up and tests. A process spoofing an exact command can
  add a conservative `unknown`.
- Covered elsewhere: lifecycle ownership and worker startup belong to ADR-0574;
  build identification and verdict policy belong to ADR-0482.

## Threat model

- Boundary inventory: the added read crosses from the host process table into
  a test warning. No network or permission boundary is widened.
- Actor model: a local host user can influence their own process arguments;
  the operator controls the test checkout and stack deployment.
- Control per boundary: fixed argv, command deadline and byte cap bound the read;
  exact process-name and argument checks prevent loose matches. Only counts and
  generic failure reasons reach warnings, never raw command lines.
- Out of scope: a hostile privileged host can falsify its process table; host
  integrity remains the operator's responsibility.
