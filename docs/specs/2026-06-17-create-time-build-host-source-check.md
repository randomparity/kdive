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
  checks, so the build-time behavior is unchanged and the create-time rejection is
  indistinguishable from a (now-unreachable in normal flow) build-time rejection.
- `resolve_and_admit` replaces its inline `if host.kind ...` block with a call to
  this helper, computing `is_git = is_git_source(parsed_profile)` as before. No
  behavior change at build time; the build-time check remains the defense-in-depth
  backstop because the host row is operator-mutable between create and build.

### The create-time call site (`mcp/tools/lifecycle/runs/create.py`)

After the three unconditional preconditions pass and before the Run is inserted
(inside `_create_locked`, under the held ALLOCATION/SYSTEM/INVESTIGATION locks), the
create path:

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

The check runs after the live-run precondition so an already-occupied System still
returns its `transport_conflict` first (precondition order is load-bearing — a stale
System must not leak a compatibility error before its own block fires).

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
- **Single rule:** the compatibility matrix and its two error strings are defined
  once (`check_source_kind_compatibility`) and both call sites invoke it.

## Out of scope

- `runs.cancel` (#535) — recovering an already-stranded run.
- Surfacing each host's accepted source kind on a read tool (#536).
- Source/`KERNEL_SRC` content validation beyond the host-kind ↔ provenance pair (#532).
- Re-checking host existence/enablement/reachability/capacity at create time — these
  stay build-time, as they are mutable between create and build.
- Any schema, migration, or new error category (none required).
