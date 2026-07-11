# Create-time build-host ↔ kernel-source compatibility check (#534)

- **Status:** Draft
- **Date:** 2026-06-17
- **ADR:** [0157](../adr/0157-create-time-build-host-source-check.md)
- **Issue:** #534

## Problem

`runs.create` accepts a build profile that pairs a remote build host (`kind` ∈
{`ssh`, `ephemeral_libvirt`}) with a non-git (warm-tree string) `kernel_source_ref`,
or a local host (`kind='local'`, including the default `worker-local`) with a git
`{git: {...}}` ref. The pairing is incompatible — ADR-0099 §5 fixes the
builder-dependent interpretation of `kernel_source_ref` per host kind — but the
rejection happens only later, at `runs.build` admission
(`services/runs/build_host_selection.py`):

```
configuration_error: a remote build host requires a git kernel_source_ref
configuration_error: a local build host requires a warm-tree kernel_source_ref, not a git ref
```

All three inputs the check needs are known at create time:

- `build_host` name — parsed from the profile (`ServerBuildProfile.build_host`,
  defaulting to `worker-local`).
- source kind — `is_git_source(profile)` is a pure function of the parsed profile.
- `host.kind` — a `build_hosts.get_by_name` lookup; `runs.create` already holds an
  open connection under the SYSTEM advisory lock when it runs its preconditions.

Today `runs.create` only validates the profile **structurally** (`BuildProfile.parse`)
and checks the live-run precondition. The incompatible run is inserted as `CREATED`,
occupies the System with a non-terminal run, and — absent `runs.cancel` (#535) —
forces a teardown to recover. ADR-0099 documents the fail-closed cross-checks but
gives no rationale for *deferring* them to build time; the timing is incidental.

## Constraint: the compatibility rule must stay single-sourced

The matrix lives today inline in `resolve_and_admit`. Re-deriving it at the create
boundary would duplicate the rule and its two error messages, so the two sites could
drift. The rule MUST be factored into one helper that both the create-time check and
the build-time check call, so there is exactly one definition of "which source kinds
a host kind accepts" and one set of error strings.

This also leaves a clean seam for two sibling issues that build on it:
- #536 wants to surface each host's accepted source kind(s) to callers.
- #532 wants source/`KERNEL_SRC` validation pushed to admission or earlier.

Both want to read the *same* mapping; a single helper is the thing they import.

## Design

### The reusable helper (`services/runs/build_host_selection.py`)

A pure function checks one `(host_kind, is_git)` pair and raises on a mismatch:

```python
def check_source_kind_compatibility(
    *, host_kind: BuildHostKind, is_git: bool, build_host: str
) -> None:
    """Raise CONFIGURATION_ERROR when host_kind is incompatible with the source kind.

    LOCAL accepts a warm-tree string only; SSH/EPHEMERAL_LIBVIRT accept a git ref
    only. The build_host name is carried into the error details for the caller.
    """
```

- Inputs are primitives (`BuildHostKind`, a `bool`, the host name string) — no DB
  connection, no profile object — so both call sites and unit tests can drive it
  directly. The host name is passed in (rather than re-derived) because the create
  path and the build path resolve the name identically (`profile.build_host or
  "worker-local"`) but hold it in different locals.
- The error messages and `category`/`details` are byte-identical to today's inline
  checks, so the build-time behavior is unchanged. For an **enabled, reachable** host
  the create-time rejection is byte-identical to the (now usually unreachable)
  build-time rejection. For a **disabled or unreachable** host the two diverge by
  design: the build path checks availability *first*
  (`build_host_selection.py:70` → "build host '<name>' is not available") and only
  then compatibility, whereas the create path deliberately skips availability (it is
  mutable between create and build) and reports the compatibility mismatch. Both
  failures share the `configuration_error` category — only the `detail` string and
  `details` keys differ. This divergence is an accepted consequence: create rejects
  the one thing that is wrong *regardless of when the run is built* (the pairing),
  and a host that is disabled now may be re-enabled before build, so availability is
  not create's to assert. An operator who sees a clean `runs.create` followed by a
  "not available" `runs.build` is seeing the host's availability change between the
  two vantages — the intended division of labor, not a regression.
- `resolve_and_admit` replaces its inline `if host.kind ...` block with a call to
  this helper, computing `is_git = is_git_source(parsed_profile)` as before. No
  behavior change at build time; the build-time check remains the defense-in-depth
  backstop because the host row is operator-mutable between create and build.

### The create-time call site (`mcp/tools/lifecycle/runs/create.py`)

`_create_locked` runs, in order under the held ALLOCATION/SYSTEM/INVESTIGATION
locks: (a) the three unconditional preconditions (`_preconditions_block_response`:
System reachable, allocation live, single project, one-run-per-System), (b) the
optional reuse-requirement snapshot assertion (`_assertion_block_response`), (c) the
`INVESTIGATION_OPEN_FOR_RUN` state check, then (d) `_insert_run`. The compatibility
check is inserted **between (c) and (d)** — after the investigation-state check and
immediately before `_insert_run`. This placement is load-bearing:

- It runs **after** every existing precondition and assertion, so a stale/conflicting
  System (`transport_conflict`), an unmet reuse requirement, or a non-open
  investigation each surfaces its own error first; the compatibility error never
  pre-empts or leaks past a more fundamental block.
- It runs **before** `_insert_run`, so a mismatch inserts no Run and performs no audit
  write or investigation flip (those happen in `_insert_run` /
  `_flip_investigation_if_open`).

At that point the create path:

1. Skips the check entirely when the parsed profile is **not** a server-build
   profile (external-build lane has no `kernel_source_ref` and no `build_host`).
2. For a server profile, resolves the host by name (`get_by_name`) on the connection
   it already holds.
3. If the host is **absent**, the create path does **not** reject — host existence is
   re-validated at build time, and an operator may register the named host between
   create and build. Create only rejects a *known* incompatible pairing. (This keeps
   create's failure surface to the one thing #534 is about: a definitively
   incompatible pair. Host existence, enablement, reachability, and capacity stay
   build-time concerns — they are mutable and time-of-build is the correct vantage.)
4. If the host exists, calls `check_source_kind_compatibility(...)`. A mismatch
   returns the identical `configuration_error` envelope and inserts **no** run.

`runs.create` already imports nothing from `build_host_selection`; it gains an import
of the new helper and of `get_by_name`/`BuildHostKind`.

## Acceptance

- **New, create-time:** `runs.create` with a remote (`ssh` or `ephemeral_libvirt`)
  build host + a warm-tree string `kernel_source_ref` returns
  `configuration_error` ("a remote build host requires a git kernel_source_ref") and
  inserts **no** run (the System has zero non-terminal runs afterward).
- **New, create-time:** `runs.create` with a local host (default `worker-local`) + a
  git `{git:{...}}` ref returns `configuration_error` ("a local build host requires a
  warm-tree kernel_source_ref, not a git ref") and inserts no run.
- **Valid combos still accepted:** local host + warm-tree string, and remote host +
  git ref, both create successfully (status `created`).
- **Absent named host is not rejected at create:** a server profile naming a host
  that does not exist still creates (host existence is a build-time concern); the run
  is inserted `CREATED`.
- **External-build profile creates:** a `source="external"` profile (no
  `kernel_source_ref`) is unaffected and creates.
- **Build-time backstop intact:** the existing build-time rejection still fires when
  the host row's `kind` is mutated between create and build (e.g. a profile created
  against an `ssh` host with a git ref, then the host's kind flipped to `local`
  before build) — `runs.build` returns the same `configuration_error`, no job
  enqueued.
- **Precondition order preserved:** a System that already has a live run, paired with
  an incompatible profile, returns `transport_conflict` (the live-run block), **not**
  the compatibility error — the compatibility check runs after every existing
  precondition/assertion.
- **Disabled-host divergence is intended:** an incompatible profile against a
  disabled/unreachable host returns the **compatibility** `configuration_error` at
  create (availability is not asserted at create), while build returns the
  **availability** `configuration_error` ("not available"). Both are
  `configuration_error`; the detail strings differ by design.
- **Single rule:** the compatibility matrix and its two error strings are defined
  once (`check_source_kind_compatibility`) and both call sites invoke it.

## Out of scope

- `runs.cancel` (#535) — recovering an already-stranded run.
- Surfacing each host's accepted source kind on a read tool (#536).
- Source/`KERNEL_SRC` content validation beyond the host-kind ↔ provenance pair (#532).
- Re-checking host existence/enablement/reachability/capacity at create time — these
  stay build-time, as they are mutable between create and build.
- Any schema, migration, or new error category (none required).
