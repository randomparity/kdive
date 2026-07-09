# 0318 — Advertise debug-feature kernel-config requirements; arm only supported features

- **Status:** Accepted
- **Date:** 2026-07-08
- **Issue:** #1052
- **Spec:** [Spec 3 of 3](../superpowers/specs/2026-07-08-debug-feature-config-gate-1052-design.md)
- **Builds on:** ADR-0316 (remove server-build lane), ADR-0317 (image kernel-config offer)

## Context

The build/config redesign made agent-builds-locally + upload-only the sole kernel lane.
Spec 1 deleted all `.config` validation but kept `effective_config` (the agent's `.config`)
as an accepted-but-unread upload. kdive has no runtime knowledge of the booted kernel's
config: feature gating uses profile bits + live probes, never the kernel's real `CONFIG_*`.
So the agent is never told which symbols a debug feature needs, and kdive arms features the
uploaded kernel cannot support — failures surface late (a crash that never dumps, a sysrq
that does nothing).

## Decision

Introduce a `kernel_config` package holding a single declarative registry of
feature → `CONFIG_*` requirements, a pure `.config` parser, a pure support check, and a read
helper for the uploaded artifact. Each feature carries two clause lists (each clause an
OR-group): an **`advertised`** superset shown by the tool, and a deliberately narrower
**`gate_required`** subset the gate refuses on. Keeping them separate is load-bearing: the
advertised set is guidance ("everything worth building"), the gate set is "the kernel
provably cannot do this without these." Gating on the superset would refuse working kernels —
e.g. `RANDOMIZE_BASE` (KASLR) is routinely disabled on debug kernels and kdump works without
it, so it is advertised but not gated; the two kexec load syscalls are an OR-group so a
kernel with either passes. Use the registry two ways:

1. **Advertise** — a static read-only tool `catalog.feature_config_requirements` returns the
   full manifest (feature, summary, `gated`, OR-group requirements). Advisory; cross-linked
   from `runs.create` / `artifacts.expected_uploads` `suggested_next_actions`. The agent
   decides what to build.
2. **Gate** — at the three config-dependent arming seams (kdump crashkernel reservation in
   `install`; kdump-method vmcore fetch; sysrq diagnostic), fetch the Run's uploaded
   `effective_config`, parse it, and **refuse the action with `CONFIGURATION_ERROR` naming
   the missing symbols** when a required clause is provably unmet.

Two boundary rules:

- **The gate fails open.** `load_effective_config` returns "cannot check, arm as today" for
  an absent artifact row, an unconfigured/unreachable store or a raising fetch, and a
  degenerate (zero-enabled-symbol) config that signals a truncated/wrong-file upload. A
  benign advisory read never converts into an install/vmcore/sysrq failure, and kdive does
  not verify config↔kernel correspondence, so a stale config cannot block a working kernel.
- **Absent config arms as today.** `effective_config` is optional and commonly absent; kdive
  cannot prove a missing feature, so it does not gate. R2 applies only when a config exists.
- **gdbstub is not gated.** The QEMU gdbstub attaches to vCPU state regardless of guest
  config and is armed at provision time before any kernel is uploaded, from a seam with no
  DB/store. It is advertised (via `debuginfo`) but never disabled — a deliberate deviation
  from the issue's literal four-seam list.

No schema change: the gate reads the existing `effective_config` artifact row + object.

## Consequences

- The agent gets an advisory contract (build these symbols for these features) and, when it
  builds without them, a loud categorized refusal at the exact action instead of a silent
  late failure.
- kdive gains its first runtime read of the kernel's own config — a new gating input beside
  profile bits and live probes.
- A `SENSITIVE` artifact is now read server-side; only derived booleans and `CONFIG_*` names
  (public knowledge) leave the seam — never the config bytes.
- host_dump vmcore capture stays ungated (host-side, needs no guest config); only the KDUMP
  method gates on `crash_capture`.
- The registry is advisory; drift over-advertises harmless extra symbols or over-refuses an
  explicitly-requested feature (surfaced by the named-symbol reason), never silently mis-boots.

## Considered & rejected

- **Gate gdbstub literally** — the raw stub works without any kernel config and is armed
  before the kernel exists; gating it would refuse a working capability and is
  ordering-impossible.
- **Disable all features when no config is uploaded** — a strict reading of R2 that breaks
  every current kdump/sysrq/vmcore flow, since the upload is optional.
- **A run-scoped "will my config work" tool** — duplicates what the agent already computes
  from its own `.config` and pushes a SENSITIVE read onto a response path.
- **A cached supported-features column** — adds a migration and a staleness surface; the
  per-run object read is cheap.
- **Silently disabling instead of refusing** — a silent no-op on an explicit agent action
  reads as success; categorized refusal is diagnosable.
- **Gating on the advertised superset** — refuses working kernels (KASLR-off debug kernels;
  kernels with only one kexec load syscall); the gate keys on a minimal `gate_required`
  subset instead.
- **Failing the arming action on a config-read/degenerate config** — turns a benign advisory
  read into a new failure surface; the gate fails open.
