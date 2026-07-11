# ADR 0299 — Iterate the boot cmdline at install time without a kernel rebuild

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** kdive maintainers

## Context

Kernel cmdline extra args (e.g. `dhash_entries=1`) are bound at **build** time. `runs.build`,
`runs.build_install_boot`, and `runs.complete_build` accept a `cmdline` that is persisted into the
`build` step result (`BuildStepResult.cmdline`); `cmdline_for` (`services/runs/steps.py`) reads
that value at install time and appends it to the platform-required tokens
(`system_required_cmdline`: `console=ttyS0`, local `root=/dev/vda`, and the method-dependent
`crashkernel=256M` / `nokaslr`). `runs.boot`'s docstring states the rule: *"The kernel cmdline is
fixed at build time … do not pass them here."*

Sweeping boot-parameter variants (`dhash_entries=1`, then `=2`, …) — the natural motion for the
`OPUS_REVIEW.md` §5 I-8 test case — therefore forces a full rebuild per variant, even though the
kernel image is identical. Build is the expensive step.

Two idempotency layers make "just add a `cmdline` param to `runs.install`" a silent no-op on the
second install:

- **Single-row terminal ledger.** `run_steps` has `UNIQUE (run_id, step)` — one `install` and one
  `boot` row per Run. `claim_run_step` returns `claimed=False` (replay the stored result) once the
  step is `SUCCEEDED`, so the install handler's provider side effect never re-runs.
- **Single-use job dedup.** `queue.enqueue` dedups on `{run_id}:install` / `{run_id}:boot`; a
  re-enqueue returns the existing succeeded job. Only `retry_terminal_failed=True` recycles today,
  and only a `failed` job.

Iterating within one Run thus requires recycling both layers for `install` and `boot`.

See `docs/archive/superpowers/specs/2026-07-02-install-cmdline-iteration-988.md`.

## Decision

Relocate the cmdline entry point from the build step to the install step, with the `run_steps`
ledger as the authoritative state and the job dedup subordinate to it.

- **`runs.install` gains an optional `cmdline`** that **replaces** the build-baked extra args
  (platform tokens always preserved). Omitting it reuses the build-baked extra (unchanged
  behavior). The wrapper `Field` text enumerates the always-present, never-modifiable platform
  tokens (`console=ttyS0`, `root=/dev/vda`, `crashkernel=256M`/`nokaslr`) and the replace rule.
  A cmdline containing a platform-owned token (`root=`/`console=`/`crashkernel=`) is rejected
  `cmdline_overrides_platform_args` (the existing `platform_owned_cmdline_token` guard, shared with
  `runs.build`); a blank cmdline is rejected `cmdline_blank`.

- **A differing cmdline recycles the `install` and `boot` ledger rows.** `runs.install(cmdline=X)`
  resolves the requested effective extra (`normalize(X)`, else the build-baked extra) and, under
  the per-Run advisory lock, compares it to the extra recorded on the `install` step. Equal → a
  no-op replay. Different, `install` step `succeeded` → delete the `install` **and** `boot` ledger
  rows and enqueue a fresh `INSTALL` job carrying `X`. An `install` or `boot` step that is
  `running` → reject `step_in_progress` (never delete a row a worker is mid-flight on). The
  install handler records the applied extra (`X`, else the build-baked extra) in the `install` step
  result under `cmdline`.

- **Ledger-absent recycles the terminal job, payload and all.** The install/boot enqueue recycles
  a terminal (`succeeded` **or** `failed`) job when the step's `run_steps` row is absent,
  generalizing the current failed-only `retry_terminal_failed`. This is the single mechanism that
  both preserves the existing failed-retry path (a failed step's row is already deleted by
  `abandon_run_step`) and, after a re-stage, lets `runs.boot` recycle the stale `succeeded` boot
  job so it re-power-cycles into the new cmdline. The recycle **overwrites the job payload** on the
  reset, not only its state: today's recycle `UPDATE` touches state fields only (harmless when the
  retried payload is byte-identical), but a re-stage supplies a new cmdline, so the recycled
  `INSTALL` job must carry it — otherwise it re-runs the prior cmdline and silently boots the wrong
  variant. `canceled` jobs stay untouched.

- **`runs.get` surfaces the installed cmdline.** The applied client extra recorded on the `install`
  step is surfaced as `data.installed_cmdline` beside the existing `data.required_cmdline`, so the
  sweep loop can confirm which variant is live rather than being write-only.

- **`InstallPayload(RunPayload)`** carries `cmdline: str | None` (non-blank validator, mirroring
  `BuildPayload`); `runs.boot` keeps the bare `RunPayload`. `cmdline_for` gains an `override`
  parameter (replace when set, append build-baked when not). The composite
  `runs.build_install_boot` path is untouched (build-time cmdline still valid); remote-libvirt
  install already threads `InstallRequest.cmdline`, so it needs no change.

## Consequences

- An agent sweeps boot-parameter variants against one built kernel with
  `install(A)→boot→install(B)→boot`, no rebuild — the acceptance criterion.
- The `run_steps` ledger becomes the explicit source of truth for step completion; the job dedup
  key is a subordinate cache of the current attempt. This is a small conceptual shift but matches
  what the code already does (a failed step deletes its ledger row to force a retry).
- The ledger holds only the **current** install/boot; there is no per-variant boot history. The
  live variant is observable via `runs.get` `data.installed_cmdline`. Each `runs.install` is
  audited with the cmdline folded into its (one-way) `args_digest`, so distinct variants are
  distinguishable as distinct operations, but the audit does not retain the readable cmdline
  strings — per-variant history is out of scope (see the spec).
- Re-staging is rejected while a step is `running`; a sweeping agent must let the prior
  install/boot settle (poll to terminal) before the next variant. This trades a small amount of
  concurrency for freedom from mid-flight ledger/job races.
- Reusing one `idempotency_key` across variants replays the first envelope and silently ignores
  later cmdlines; the tool doc directs the agent to omit the key or vary it per variant.

## Considered & rejected

- **A new Run per variant, reusing the built `kernel_ref`.** Avoids ledger/job recycling but
  needs `kernel_ref`-reuse plumbing across Runs and diverges from the issue's "iterate install/boot
  on one Run" framing; multiplies Run rows per sweep. The single-Run recycle is closer to the
  existing grain.
- **An explicit `reinstall=true` / `force` flag.** A second knob the agent must remember to set on
  every iteration; the cmdline value itself already encodes intent (a different cmdline means
  re-stage), so the flag is redundant.
- **A generation counter in the dedup key (`{run_id}:install:{n}`).** Would need a new place to
  persist `n`; and it does not help — the single-row ledger still blocks the handler regardless of
  job identity, so the ledger must be recycled either way. Recycling the ledger alone is sufficient.
- **Deleting the succeeded job rows on re-stage.** Works (no FK references `jobs`, audit is a
  separate table) but introduces a job-deletion path with no precedent and would 404 a
  still-referenced job id. Recycling the terminal job to `queued` when the ledger row is absent
  stays within the existing `retry_terminal_failed` grain.
- **Append instead of replace.** Appending the install cmdline after the build-baked extra yields
  confusing duplicate tokens (kernel last-wins) and couples each sweep to whatever build baked.
  Replace makes each iteration independent and predictable; the agent can restate any build arg it
  wants to keep. Platform tokens are preserved either way.
- **A `cmdline` on `runs.boot`.** Boot power-cycles whatever install defined; putting the knob on
  boot would split cmdline ownership across two planes. Iteration stays an install-plane concern.
- **No re-stage guard (queue behind an in-flight boot).** Deleting a ledger row a worker is
  mid-flight on races `complete_run_step`; rejecting `step_in_progress` is the simple, safe
  contract.
