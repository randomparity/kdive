# ADR 0161 — Admit a local warm-tree build only when `KDIVE_KERNEL_SRC` is usable

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0099](0099-remote-build-host-targets.md)
  (the `build_hosts` inventory + the §5 host-kind ↔ source-kind fail-closed matrix),
  [ADR-0157](0157-create-time-build-host-source-check.md) (the shared
  `check_source_kind_compatibility` helper and the create-time/build-time
  two-checks-one-rule pattern this mirrors), [ADR-0087](0087-config-registry.md)
  (the `KDIVE_*` typed-config registry and its per-process scoping).
- **Spec:** [`../specs/2026-06-17-local-warm-tree-build-admission.md`](../specs/2026-06-17-local-warm-tree-build-admission.md)
- **Issue:** [#532](https://github.com/randomparity/kdive/issues/532)

## Context

A warm-tree `runs.build` targeting the seeded local host (`worker-local`,
`kind='local'`) materializes its workspace by mirroring the worker-process setting
`KDIVE_KERNEL_SRC` into scratch (`sync_tree`,
`providers/shared/build_host/workspace.py`). `KDIVE_KERNEL_SRC` defaults to `""` and
is **worker-scoped** (`processes=_WORKER`, `config/core_settings.py`): no
`systems.toml`, example, or seed sets it, so a fresh demo deploy has a registered
local build host that cannot build anything. The empty value passes
`config.require()` (which rejects only `None`, not `""`) and is bound into the build
closure at worker composition; the failure surfaces only when the BUILD job runs
`sync_tree`, with the message `KERNEL_SRC_UNSET_DETAIL`.

ADR-0157 moved the host-kind ↔ *source-kind* compatibility check to `runs.create` and
kept a build-time backstop, factoring the rule into `check_source_kind_compatibility`.
Its spec named #532 as the sibling that wants the `KDIVE_KERNEL_SRC` *value* validated
"at admission, or earlier." #532's issue text suggests `runs.build` admission. That
boundary is **not available** for this value: `runs.build` runs in the **server**
process; `KDIVE_KERNEL_SRC` is read only in the **worker** at builder composition
(`LocalLibvirtBuild.from_env` → `make_checkout`). The canonical demo topology
(`docker-compose.yml`) runs `server` and `worker` as separate services with separate
`environment:` blocks — the server's environment does not, and by the ADR-0087
process-scoping contract should not, carry `KDIVE_KERNEL_SRC`. A server-side read
would be a false guarantee: green at create/build admission on a server that has the
var set, yet still dead on a worker that does not.

So there are two distinct failures the issue conflates, with two distinct correct
homes:

1. **Late validation.** The empty/invalid `KDIVE_KERNEL_SRC` is caught only deep inside
   the build job's checkout side effects (`sync_tree`, after the per-run workspace
   `mkdir` and inside the rsync seam). It should be caught at the worker's *admission*
   of the job into execution — before any workspace materialization — so the BUILD
   job fails fast and locally, the same shape ADR-0157 gave the source-kind rule.
2. **No working demo path.** Nothing stages a tree or sets `KDIVE_KERNEL_SRC` for
   `worker-local`, so the headline local lane is dead out of the box.

## Decision

**(1) Validate the warm-tree source at the dispatch `LOCAL` branch, reusing the
existing message.** Factor `sync_tree`'s leading emptiness/usability guard into a small
pure predicate in `providers/shared/build_host/workspace.py` (where the two message
constants already live), so both `sync_tree` and a new admission helper call it with no
string duplicated. Add the admission helper beside ADR-0157's in
`services/runs/build_host_selection.py`; it raises the existing
`KERNEL_SRC_UNSET_DETAIL` / `KERNEL_SRC_INVALID_DETAIL` (`CONFIGURATION_ERROR`) when the
predicate reports an offending value.

Call it from `run_build_on_host` (`providers/shared/build_host/dispatch.py`) at the top
of its existing `if host.kind is BuildHostKind.LOCAL:` branch, before
`asyncio.to_thread(builder.build, ...)`. That branch is the single seam that already
discriminates LOCAL from transport and is the earliest point that both knows the host is
LOCAL and is about to run the warm-tree build — so the check fires before any workspace
side effect (`build_workspace` → per-run `mkdir` → rsync). It is structurally a no-op
for non-`LOCAL` hosts (the transport/git path is a different branch and never reads
`KDIVE_KERNEL_SRC`). The worker BUILD handler frames (`_build_and_record`/`_run_build`)
are deliberately **not** the call site: they hold only the `builder` object, never
`kernel_src`, and do not branch on `host.kind`.

`KDIVE_KERNEL_SRC` is read once, by the worker BUILD handler (`jobs/handlers/runs.py`,
which already runs in the worker process and imports config), via `config.get`; the
handler threads the value through `_run_build` into `run_build_on_host`, which forwards
it to the admission helper. We do not read config inside `dispatch.py`/`workspace.py`
(that package follows a "value passed in, never read from config" convention, which this
preserves), and we do not widen the `Builder` port to expose the closure-captured value.
In a live worker `config.load()` snapshots the env once at startup and is not reset
during operation, so the handler's read and the composition-time read resolve against the
same worker snapshot, making the rejection byte-identical to the backstop by
construction.

`sync_tree`'s in-place check **stays** as a defense-in-depth backstop, exactly as
ADR-0157 kept its build-time check: the staged tree can be unmounted, deleted, or made
non-absolute between admission and the rsync, and `sync_tree` remains the authority on
the bytes it is about to mirror. Two checks, one predicate, one pair of messages.

The admission boundary for *this value* is the **worker dispatch LOCAL branch**, not
`runs.build` server admission, because that is the earliest point at which the
authoritative value (`KDIVE_KERNEL_SRC` in the worker's own environment) is in scope.
This is consistent with ADR-0157's principle — validate at the earliest boundary that
*holds the inputs* — applied honestly to a worker-scoped input. Because the rejection
propagates through `_build_and_record`'s existing `except CategorizedError` and a LOCAL
host holds no build-host lease, the lease/retry contract is unchanged; only the timing
of the identical failure moves earlier.

**(2) Give the demo a documented one-step bootstrap, not a bundled tree.** Extend
`docs/operating/build-source-staging.md` with an explicit demo/compose bootstrap: a
host bind-mount of a kernel tree into the `worker` service plus
`KDIVE_KERNEL_SRC=<mount path>`, shown as a commented, copy-ready stanza in
`docker-compose.yml`'s `worker` service. We do **not** bundle or auto-download a
kernel tree (multi-hundred-MB, license-laden, version-coupled); we make the existing
operator prerequisite discoverable and one-step for the demo. #533 tracks the
diagnostics surfacing of this gap and is out of scope here.

## Consequences

- A warm-tree local build with empty/invalid `KDIVE_KERNEL_SRC` fails at the dispatch
  `LOCAL` branch with the existing `CONFIGURATION_ERROR` message, before the per-run
  workspace is created or rsync runs — fast, local, and byte-identical to the old
  build-time error. No new message, no new error category.
- The fix lives where the authoritative value lives (the worker), so it is honest
  under the split server/worker topology the demo itself uses. We explicitly reject a
  server-side `runs.build` read as a false guarantee.
- `sync_tree` keeps its check as a backstop; behavior on the (now usually
  unreachable) deep path is unchanged. Two checks, one predicate, one pair of
  messages.
- The compatibility helper module gains a second small pure helper next to
  ADR-0157's; both are unit-testable on primitives with no DB or profile object.
- The demo gains a documented, copy-ready local build path. No kernel bytes enter the
  repo or images; the worker image's build toolchain (ADR-0146) already exists.

## Considered & rejected

- **Validate at `runs.build` server admission (the issue's literal suggestion).**
  Rejected: `KDIVE_KERNEL_SRC` is worker-scoped (ADR-0087) and the demo runs server
  and worker as separate services with separate env. A server read is either
  unavailable (var unset on the server) or a false pass (var set on the server but not
  the worker). It would validate the wrong process's environment.
- **Validate at `runs.create` (alongside ADR-0157's source-kind check).** Rejected for
  the same reason, more so: create is even further from the worker, and the value is
  mutable up to build time. ADR-0157 deliberately put only the *pure* source-kind
  check at create; the `KDIVE_KERNEL_SRC` value is not a pure function of the profile.
- **Require `KDIVE_KERNEL_SRC` at worker startup (`config.validate("worker")` /
  `required_when`).** Rejected: a worker that services only git/remote build lanes
  legitimately has no warm tree and must not be forced to set the var. The requirement
  is conditional on *a warm-tree LOCAL build actually being admitted*, which startup
  cannot know.
- **Drop the `sync_tree` check now that admission covers it.** Rejected for the same
  defense-in-depth reason ADR-0157 kept its backstop: the tree is operator-mutable
  between admission and the rsync. Two checks, one rule.
- **Bundle or auto-stage a kernel tree for the demo.** Rejected as the default:
  hundreds of MB, license and version coupling, and it hides the real operator
  prerequisite. A documented one-step bind-mount + env is lower-risk and teaches the
  real contract. (An operator who wants a turnkey tree can follow the same doc.)
