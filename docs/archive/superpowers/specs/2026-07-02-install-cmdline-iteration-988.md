# Install-time boot-cmdline iteration without a kernel rebuild (#988)

- **Issue:** #988 (`OPUS_REVIEW.md` §5, item I-8, Tier 3)
- **ADR:** [ADR-0299](../../adr/0299-install-cmdline-iteration.md)
- **Status:** Draft

## Problem

The kernel cmdline extra args (e.g. `dhash_entries=1`) are bound at **build** time:
`runs.build` / `runs.build_install_boot` / `runs.complete_build` accept a `cmdline`
that is persisted into the `build` step result (`BuildStepResult.cmdline`), and
`cmdline_for` (`services/runs/steps.py`) reads that build-anchored value at install time
and appends it to the platform-required tokens. The `runs.boot` docstring states the rule
outright: *"The kernel cmdline is fixed at build time … do not pass them here."*

Sweeping boot-parameter variants — the natural motion for test case 05 (`dhash_entries=1`,
then `dhash_entries=2`, …) — therefore requires re-invoking the whole build for each variant,
even though the kernel image is unchanged. Build is the expensive step; install and boot are
cheap.

## Goal / acceptance

Let an agent apply a **fresh cmdline against an already-built kernel** through
`runs.install` and boot it, without a rebuild.

Acceptance (from the issue): **two boots of one built kernel with different `dhash_entries`
values, no rebuild.** Concretely, this sequence succeeds and boots two distinct cmdlines:

```
runs.install(run, cmdline="dhash_entries=1") ; runs.boot(run)   # boot A
runs.install(run, cmdline="dhash_entries=2") ; runs.boot(run)   # boot B
```

with the `build` step run exactly once.

## Constraints from the existing design (why this is not just a parameter add)

Two idempotency layers make a naive "add a `cmdline` param" a no-op on the second install:

1. **The `run_steps` ledger is single-row and terminal.** `run_steps` has
   `UNIQUE (run_id, step)`, so there is exactly one `install` row and one `boot` row per Run.
   `claim_run_step` returns `claimed=False` (replay the stored result) once the step is
   `SUCCEEDED`, so the install handler's provider side effect never re-runs.
2. **The job dedup key is single-use.** `queue.enqueue` dedups on `{run_id}:install` /
   `{run_id}:boot`; a re-enqueue returns the existing (succeeded) job untouched. Today only
   `retry_terminal_failed=True` recycles — and only a `failed` job.

So iterating within one Run requires **recycling both layers** for `install` and `boot`.

## Decision summary

The `run_steps` ledger is authoritative; the job dedup is subordinate. A fresh cmdline that
differs from the currently-installed one **recycles the `install` and `boot` ledger rows**;
the enqueue machinery is generalized so an **absent ledger row recycles the terminal
(succeeded-or-failed) job**, letting the existing install/boot flow re-run. No new job-deletion
path, no generation counter, no schema change. Detail in
[ADR-0299](../../adr/0299-install-cmdline-iteration.md).

## Agent-facing contract

`runs.install(run_id, cmdline=None, idempotency_key=None)` — the `cmdline` parameter is new.

- **Semantics: replace, not append.** When `cmdline` is supplied it **replaces** any
  build-time extra args. When omitted, install reuses the build-baked extra (today's behavior).
  Platform-required tokens are **always present and never modifiable**, regardless of `cmdline`.
- **The `Field` description must enumerate the always-present / never-modified args** so the
  agent knows exactly what it may not touch:
  - `console=ttyS0` — always present (serial-console capture parity).
  - `root=/dev/vda` — always present on local-libvirt (direct-kernel root device).
  - `crashkernel=256M` — present when the System's capture method resolves to kdump.
  - `nokaslr` — present when the method resolves to gdbstub.
  A `cmdline` that contains any platform-owned token (`root=`, `console=`, `crashkernel=`) is
  rejected `CONFIGURATION_ERROR`, `data.reason = "cmdline_overrides_platform_args"`,
  `data.token = "<token>"` — identical to the `runs.build` guard
  (`platform_owned_cmdline_token`).
- **Blank cmdline** (empty or whitespace-only) is rejected `CONFIGURATION_ERROR`,
  `data.reason = "cmdline_blank"` (a blank value is a caller mistake, distinct from omitting the
  argument).
- **Sweep note in the tool doc:** to sweep variants, omit `idempotency_key` (or use a distinct
  key per variant). Reusing one key replays the prior envelope and ignores the new `cmdline`
  (standard replay-idempotency semantics).
- `runs.boot`'s docstring is updated: iteration now happens on `runs.install`; the "fixed at
  build time" wording is removed. `runs.boot` itself still takes no `cmdline`.
- **Read-back.** `runs.get` already advertises `data.required_cmdline` (the platform tokens). It
  additionally surfaces `data.installed_cmdline` — the applied client extra recorded on the
  `install` step (`null` before the first install, or when install baked no extra). Without this
  the sweep loop is write-only: an agent has no API-level confirmation of which variant is live
  after a re-stage, so a mis-apply (or the payload-recycle bug above) would be undetectable
  through the tool surface.

## Re-stage state machine

`runs.install(cmdline=X)` resolves the **requested effective extra**:
`normalize(X)` if `X` is given, else the build-baked extra recorded on the `build` step.

**Normalization is pinned.** `normalize` is a single leading/trailing whitespace strip (the same
transform `BuildPayload._nonblank_cmdline` applies). The applied extra is stored
**already-normalized** on the `install` step, and the re-stage test compares the
identically-normalized requested value, so equality is exact — a whitespace-only difference is
neither a spurious re-stage nor a missed one.

Under the per-Run advisory lock:

| Current `install` step | Requested extra vs. recorded | Action |
|---|---|---|
| absent (`pending`) | — | first install: enqueue `INSTALL` carrying `X` |
| `succeeded`, recorded **==** requested | equal | idempotent no-op (replay existing envelope) |
| `succeeded`, recorded **!=** requested | differ | **re-stage**: delete `install` + `boot` ledger rows, enqueue a fresh `INSTALL` carrying `X` (the terminal job is recycled **payload-and-all**, see Plumbing) |
| `running` | — | reject `CONFIGURATION_ERROR`, `data.reason = "step_in_progress"` |

If the `boot` step is `running`, `runs.install` also rejects `step_in_progress` — re-staging
must not delete a ledger row a worker is mid-flight on. Re-staging only ever deletes ledger rows
whose step is settled (`succeeded`), so no worker is touching the job being recycled.

Recording: the `install` step result records the applied client extra as `cmdline` (the
`system_id` field it records today is retained). The recorded extra is the value re-stage
compares against.

After a re-stage, the agent polls the returned fresh `INSTALL` job to `succeeded` (install
redefines the domain with the new cmdline), then calls `runs.boot`. Because the `boot` ledger
row was deleted, `runs.boot`'s enqueue sees an absent ledger row and recycles the stale
`succeeded` boot job, re-power-cycling into the new cmdline. `runs.boot`'s existing
`install-first` gate holds: it waits for the fresh `install` step to be `succeeded`.

## Plumbing

- **`InstallPayload(RunPayload)`** — new payload with `cmdline: str | None = None` and the same
  non-blank validator as `BuildPayload`. `runs.install` enqueues `InstallPayload`; `runs.boot`
  keeps `RunPayload` (boot needs no cmdline).
- **Ledger-driven, payload-carrying recycle.** The install/boot enqueue recycles a terminal
  (`succeeded` **or** `failed`) job when the step's `run_steps` row is absent, replacing the
  current `retry_terminal_failed=True` (failed-only). This subsumes the existing failed-retry
  path (a failed step's row is deleted by `abandon_run_step`, so the row is already absent when a
  retry re-enqueues).

  **The recycle must overwrite the job payload**, not only reset its state. Today
  `queue.enqueue`'s recycle is `INSERT … ON CONFLICT DO NOTHING` (the new payload is discarded on
  conflict) followed by an `UPDATE … SET state='queued', attempt=0, …` that touches state fields
  **only**. That was invisible for the failed-retry case because the retried payload is
  byte-identical, but a re-stage supplies a **new cmdline** — so the broadened recycle `UPDATE`
  must also `SET payload = <new>` on the `succeeded|failed → queued` transition. Otherwise the
  recycled `INSTALL` job re-runs with the **prior** cmdline and the sweep silently boots the wrong
  variant. Recycling a `succeeded` job (new to this change — the old flag only touched `failed`)
  must additionally clear its success-only fields — `result_ref = NULL` — alongside the
  state/attempt/lease reset, so the re-queued job is not observed carrying the prior run's result.
  `canceled` jobs stay untouched (no resurrection). The flag has one caller today
  (`_enqueue_step`), so broadening its semantics is contained.
- **`cmdline_for(conn, run, method, *, root_cmdline, override=None)`** — when `override` is set,
  return `f"{required} {override}"` (replace build extras); else today's build-baked append. The
  install handler passes `override=payload.cmdline`.
- **Install handler** records the applied client extra (`override` if given, else the build-baked
  extra), already-normalized, into the `install` step result under `cmdline`.
- **`runs.get` read-back.** The `runs.get` view reads the `install` step result's recorded
  `cmdline` and surfaces it as `data.installed_cmdline` (`null` when absent). `StepProgress`
  (the ledger reader) gains the field.
- **Composite `runs.build_install_boot` is unchanged**: build-time cmdline stays valid there; its
  single `BUILD_INSTALL_BOOT` job drives install internally and does not use the standalone
  `runs.install` override path.
- **Remote-libvirt rides along for free**: its install already threads `request.cmdline`
  identically (`InstallRequest.cmdline`); no remote-specific change.

## Failure contract

| Condition | Category | `data` |
|---|---|---|
| `cmdline` contains a platform-owned token | `CONFIGURATION_ERROR` | `reason=cmdline_overrides_platform_args`, `token` |
| `cmdline` blank/whitespace | `CONFIGURATION_ERROR` | `reason=cmdline_blank` |
| `install` or `boot` step `running` | `CONFIGURATION_ERROR` | `reason=step_in_progress` |
| Run not `SUCCEEDED` / unbound / unknown | (existing) | (existing) |

## Testing

- **Unit — tool boundary (`tests/mcp/.../runs`):** `runs.install` accepts `cmdline`; enqueues an
  `InstallPayload` carrying it; rejects platform-owned token (each of `root=`/`console=`/
  `crashkernel=`) and blank cmdline with the right `reason`; rejects `step_in_progress` when a
  step row is `running`.
- **Unit — re-stage ledger (`tests/db` / service):** same effective extra → no-op (ledger row
  survives, no new job); differing extra → both ledger rows deleted and a fresh install job
  enqueued; ledger-absent recycles a `succeeded` job to `queued`.
- **Unit — `cmdline_for` override:** override replaces the build-baked extra; platform tokens
  preserved and ordered first; omitted override falls back to build-baked.
- **Unit — install handler:** passes `payload.cmdline` as override; records the applied extra
  (already-normalized) in the `install` step result.
- **Unit — recycle carries payload:** the broadened enqueue recycle resets a `succeeded`/`failed`
  job to `queued` **and overwrites its payload** — a re-staged `INSTALL` job carries the new
  cmdline, not the prior one (this is the finding-1 regression guard).
- **Unit — `runs.get` read-back:** `data.installed_cmdline` reflects the last install's applied
  extra; `null` before the first install and when no extra was applied.
- **Unit — XML:** the redefined domain `<cmdline>` carries the platform tokens + the override
  and none of the build-baked extra (replace semantics) — extends `tests/.../install`/`xml`.
- **Agent-doc/schema guards:** the `runs.install` `Field` text enumerates the platform tokens
  and the replace rule; `runs.boot` no longer claims cmdline is build-fixed. Existing
  agent-surface guards (no ADR leak, wrapper-docstring contract) stay green.
- **Live (`live_vm`, gated):** the acceptance sweep — install(`dhash_entries=1`)→boot→
  install(`dhash_entries=2`)→boot on one built kernel, asserting two distinct booted cmdlines
  and exactly one build. Runs only on the KVM host; not a PR gate.

## Out of scope

- Per-boot cmdline history / an audit of every variant booted (the ledger holds the current
  install only; `runs.get` `data.installed_cmdline` shows the live variant; each `runs.install`
  is audited with the cmdline in its one-way `args_digest`, distinguishing variants as distinct
  operations without retaining the readable strings).
- A `cmdline` on `runs.boot` (iteration is an install-plane concern; boot power-cycles whatever
  install defined).
- Capping cmdline length beyond the existing non-blank validation (parity with the build path,
  which adds no length cap).
