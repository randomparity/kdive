# Ordinary teardown refuses an authority-held domain (#2966)

Decision record: [ADR-0620 amendment (2026-09-29, #2966)](../../adr/0620-authority-owned-system-teardown.md).

## Problem

ADR-0620 says a System with durable external-boot history is torn down only by an
authority-marked TEARDOWN job, because "a completed external release stops restricting
admission but does not transfer host ownership". Two producers do not follow it:

- `enqueue_control_teardown` (`src/kdive/services/systems/authority_owned.py`) enqueues an
  unmarked teardown unless the System is a pre-activation authority-owned System. The reconciler's
  orphaned-System lane, the investigation force-close, and the break-glass teardown use it.
- The worker's `teardown_handler` (`src/kdive/jobs/handlers/systems.py`) refuses only while an
  activation *restricts* the System (`get_restricting_for_system`). After a clean release
  (`recovered`, `cleanup_complete`) it runs the ordinary provisioner against its own libvirt
  URI, finds no domain there, and commits `torn_down`.

The #2865 proof hit this path: `allocations.release`, then the orphaned-System lane, then an
ordinary teardown that succeeded while the domain kept running on the authority daemon.

## Scope

1. **Refuse in the worker.** `teardown_handler` refuses an unmarked teardown when the System has
   any external-boot activation (`get_latest_for_system`), not only a restricting one. The
   refusal is the existing terminal `CategorizedError`: category `conflict`, `details.reason`
   `external_boot_teardown_not_supported`, plus `activation_id` and `activation_state`. The
   refusal happens under the System lock, before the `tearing_down` transition, the snapshot
   reclaim, and the provisioner call. The message names `systems.teardown` as the route.
2. **Recover through the public route.** `systems.teardown` already routes a System with
   external-boot history to the authority-marked teardown. Today it returns `conflict`
   (`ordinary_teardown_fenced_by_external_boot`) when the `{system}:teardown` dedup row holds an
   ordinary job. It now replaces an ordinary prior in state `failed` with the authority-marked
   teardown (recycle policy `FAILED`). An ordinary prior in any other state keeps the conflict.
   Without this, the worker refusal leaves the dedup slot occupied, and no supported path tears
   the System down.
3. **No change to the producers.** `enqueue_control_teardown` still enqueues an unmarked job for
   a System with history; the worker refuses it and `systems.teardown` recovers it. Routing in
   the producers needs a `ProviderResolver` in every caller; that is a follow-up candidate.

The chosen outcome is **refuse**, with **route** through the existing public teardown. The ADR
amendment records the alternatives.

## Failure model

1. **Actors and deployments** — a worker process that claims TEARDOWN jobs; an operator or
   agent that calls `systems.teardown` with project `admin`; the reconciler. Deployments: local
   and remote libvirt with an external-boot provider-host authority (ADR-0584).
2. **Invariants and assets at stake** — no teardown job for a System with external-boot
   history reports `succeeded` after an ordinary provider call; the reservation credits once
   (ADR-0620); a recycled job is never still running.
3. **Accepted failure classes**
   - A System in `tearing_down` with history, left by a pre-fix ordinary teardown, makes
     `repair_stalled_tearing_down_systems` re-enqueue an unmarked job each pass; each attempt is
     refused. The cost is bounded (one refused job per pass) and the System stays visible in
     `tearing_down`. After this change no new System reaches that state with history.
   - After `allocations.release`, the System stays `ready` with a failed teardown job until an
     operator runs `systems.teardown`. This is the refusal's intended visible state.
   - An ordinary prior in state `succeeded` (the #2865 residue) still conflicts. #2965 owns
     that fixture cleanup.
4. **Covered elsewhere** — pre-activation authority-owned Systems: `enqueue_control_teardown`
   and `systems.teardown` already route them (ADR-0623). Carrier ledger and fixture removal:
   #2965. Reuse after release: #2968. Plan-less preparing teardown: #2961.

## Success

- An unmarked teardown for a System with any external-boot activation fails `conflict`,
  terminal, with the System state unchanged and no provisioner call.
- An unmarked teardown for a System with no activation runs as before.
- `systems.teardown` for a System with history and a `failed` ordinary prior returns the same
  job id, now `queued` with the authority marker.
- `systems.teardown` with a `queued` ordinary prior still returns `conflict`.

## Validation

- Handler tests in `tests/mcp/lifecycle/test_systems_tools.py` beside the existing
  `ordinary teardown is fenced by external-boot` test: a `recovered`, `cleanup_complete`
  activation refuses; a System with no activation tears down.
- Public teardown test in the same file: a `failed` ordinary prior with a retired teardown
  authority recycles to the marked job.
