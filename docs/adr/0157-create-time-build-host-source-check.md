# ADR 0157 — Validate build-host ↔ kernel-source compatibility at `runs.create`

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0099](0099-remote-build-host-targets.md)
  (the `build_hosts` inventory + the §5 fail-closed `kernel_source_ref` cross-checks
  this relocates to create time), [ADR-0070](0070-fleet-availability-system-reuse.md)
  (the `runs.create` precondition/lock structure this hooks into).
- **Spec:** [`../specs/2026-06-17-create-time-build-host-source-check.md`](../specs/2026-06-17-create-time-build-host-source-check.md)
- **Issue:** [#534](https://github.com/randomparity/kdive/issues/534)

## Context

ADR-0099 §5 made `kernel_source_ref` builder-dependent: a `local` build host accepts
only a warm-tree string, an `ssh`/`ephemeral_libvirt` host only a git
`{git:{remote,ref}}` ref, and the mismatches "fail closed." Those cross-checks were
implemented inline in `resolve_and_admit`, which runs at `runs.build` admission. But
all three inputs the check needs — the `build_host` name, the source kind, and the
host's `kind` — are fully known at `runs.create`: the name and source kind are pure
functions of the parsed profile, and `runs.create` already holds a DB connection
under the SYSTEM advisory lock. The build-time-only placement is incidental, not
designed.

The consequence is that an obviously-incompatible profile survives `runs.create`,
inserts a `CREATED` run that occupies the System, and — absent `runs.cancel` (#535) —
forces a teardown to recover. Two sibling issues (#532, #536) want to read the same
host-kind ↔ source-kind mapping, so the rule's single definition matters beyond this
fix.

## Decision

We will check build-host ↔ kernel-source compatibility at `runs.create`, after the
System/allocation/live-run preconditions and before inserting the `CREATED` run,
returning the identical `configuration_error` the build path returns. We will factor
the compatibility matrix and its two error strings into one pure helper,
`check_source_kind_compatibility`, in `services/runs/build_host_selection.py`, and
have both `resolve_and_admit` (build time) and `runs.create` call it. The build-time
check stays as a defense-in-depth backstop because the host row is operator-mutable
between create and build.

Create rejects only a *known* incompatible pairing: if the named host does not exist
at create time it is **not** rejected (host existence, enablement, reachability, and
capacity are mutable and stay build-time concerns); the external-build lane (no
`kernel_source_ref`) is skipped entirely.

## Consequences

- An incompatible profile fails at `runs.create` with a `configuration_error`,
  before any run row is inserted, so it never strands a System with an unbuildable
  non-terminal run. The failure is immediate and local to the caller's mistake.
- The compatibility matrix has exactly one definition. `resolve_and_admit` loses its
  inline `if host.kind …` block in favor of the shared helper; #532 and #536 import
  the same helper rather than re-deriving the rule. No behavior change at build time.
- `runs.create` gains a `get_by_name` lookup on the connection it already holds
  (under the SYSTEM lock), one extra round trip on the create path for server-build
  profiles. It is read-only and inside the existing transaction.
- The build-time check is now redundant in the common case but retained: the host
  row's `kind`/`enabled`/`state` can change between create and build, and `runs.build`
  remains the authority on host availability and capacity. Two checks, one rule.
- No schema, migration, or new error category. No envelope change — the create-time
  failure is byte-identical to the (now usually unreachable) build-time one.

## Alternatives considered

- **Inline the check in `runs.create`, duplicating the rule.** Fastest diff, but two
  copies of the matrix and its two error strings drift independently, and #532/#536
  would have a third and fourth copy to keep in sync. Rejected for the single-source
  helper.
- **Move the check out of `runs.build` entirely (create-only).** Removes the
  "redundant" second check. Rejected: the host row is operator-mutable between create
  and build (kind flip, disable, host removal), so build-time admission must stay the
  authority on host availability; dropping it would let a create-time-valid run reach
  a now-incompatible host. Defense-in-depth is the point.
- **Reject an absent named host at create time too.** Symmetric with build's
  `not_found`. Rejected: a host an operator has not registered *yet* is a transient,
  time-of-build condition, not the definitive incompatibility #534 targets; create
  should fail only on a pairing that is wrong regardless of when it is built.
- **Resolve and lease the host at create (full `resolve_and_admit`).** Maximal
  early validation. Rejected: leasing capacity at create would hold a build slot for
  a run that may never build, and `runs.create` is not the capacity-admission
  boundary (ADR-0099 §3 puts that synchronously at `runs.build`). Create does the
  pure, side-effect-free compatibility check only.
