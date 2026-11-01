# A failed remote module restore is re-driven on retry (#3469)

Decision: [ADR-0620](../../adr/0620-authority-owned-system-teardown.md), amended. Follows
[#3427](2026-10-30-restore-crash-window-3427-design.md). No schema or wire change.

## Problem

`DurableRemoteModuleVolumePreparationHost.execute_lifecycle` publishes a `failed-after-mutation`
lifecycle completion (no response) when the host call raises or is cancelled. `observe_lifecycle`
answers every later call carrying the same lifecycle key with `remote module lifecycle requires
recovery`, and the authority service calls it first, so the host is never invoked again. A
restore whose host call raised after the appliance rewrote the scratch volume to RESTORED is
stranded: its release CLEANUP is refused for want of `.restored`, and only a System teardown
recovers the volumes.

A second defect blocks any re-drive from succeeding: the host call receives the deadline staged by
the first call (`stage_lifecycle` keeps the first record). Once that deadline has passed,
`run_or_adopt_appliance` times out before adopting or launching and `_restore` raises, so a retry
after the original budget elapsed fails without doing work. This also affects the existing
crash retry (request staged, no completion), which then latches `failed-after-mutation`.

## Facts verified in source (main 438418763)

- `observe_lifecycle` raises for any `response is None`; `execute_lifecycle` and
  `ExternalBootAuthorityService.execute_remote_module_lifecycle` (`external_boot_authority/service.py`)
  both call it before the host. The service runs it under the System's lane lock.
- `publish_lifecycle_completion` writes `.restored` and then the completion with `_publish`, which
  refuses different bytes under an existing name. `_replace(name, expected, data)` exists for a
  compare-and-replace.
- `ConcreteRemoteModuleAuthorityHost._restore` stops the System only when running, keeps a
  formatted scratch volume, and calls `run_or_adopt_appliance` (adopt or launch), whose invocation
  is bounded by the deadline `_restore` was given; an expired one returns no result.
- Appliance restore retry table (ADR-0585, `deploy/remote_module_appliance/README.md`):
  `restore-ready` resumes the reverse rename; `restored` rewrites `restored` and removes a matching
  `O`; anything else is a recovery conflict with no further write. A failure result keeps its
  checkpoint phase. `test_restore_retry_reconciles_every_durable_terminal_transition` pins this.
- A raising host call leaves the host state a process death at that point leaves, from which #3427
  already re-runs the host.

## Options

- **A — re-drive a failed restore with this call's deadline (chosen).** A `failed-after-mutation`
  restore completion no longer answers a retry; the retry runs the host restore again with a
  deadline from its own budget (the staged one has usually expired by then), and a success
  replaces the failed completion.
- **B — document an operator remedy only.** Today's remedy is a System teardown (`observe_lifecycle`
  refuses every restore retry), which destroys the System the release was restoring.
- **C — short-circuit on a RESTORED scratch volume.** The appliance README's `restored` row rewrites
  `restored` and then removes `O`, so a RESTORED result does not prove the removal (#3427 Option C).

## Design

1. `observe_lifecycle`: a completion with no response raises `requires recovery` only for a reap;
   for a restore it returns `None`, so both callers proceed to `execute_lifecycle`.
2. `execute_lifecycle`: compute `deadline = monotonic() + request.budget_seconds` from the
   incoming request before reading the staged one. A restore's host call receives that deadline; a
   reap's keeps `admitted.local_deadline`. The staged record is not rewritten.
3. `publish_lifecycle_completion`: for a restore response, when the existing completion is exactly
   the `failed-after-mutation` bytes, replace it with `_replace`; otherwise `_publish` as now. A
   re-drive that fails again publishes the identical failed bytes, which `_publish` accepts.
4. ADR-0620 amendment recording the re-drive rule and the restore deadline.

The existing short-circuit (`.restored` present → publish its completion) now also covers a crash
after a successful re-drive wrote `.restored` but before the completion replaced the failure.

## Failure model

1. **Actors and deployments:** the remote authority service on the provider host, driven by the
   worker's release RECOVER and resolve-conflict retries; one authority instance per store
   directory (ADR-0584 lane serialization).
2. **Invariants and assets at stake:** the System's root filesystem (the appliance writes it); the
   evidence order `.restored` before a terminal completion; a terminal restore completion is never
   replaced; a reap completion is never re-driven.
3. **Accepted failure classes:**
   - A re-drive whose appliance meets a combination outside its retry table fails closed and
     publishes the same failed completion; the retry is refused again by the appliance, not by the
     store. Bounded: no write past the conflict (ADR-0585).
   - A restore retry runs for its own budget even after the first budget elapsed; each call is
     still bounded by `budget_seconds` (≤ 300).
   - `_replace` raising its existing message on a concurrent change to the completion: not
     reachable under the lane lock.
4. **Covered elsewhere:** re-driving a reap and preparation-completion recovery (operator
   exclusions, campaign 97b6ba6250ca); re-typing the `requires recovery` refusal (#3474); a retry
   against a finalized activation is refused by `refuse_finalized` in `stage_lifecycle`.

## Tests

Over the real `_Quiesce` stack: a retry after the host raised past the rewrite succeeds and a release
CLEANUP reap follows; the same retry succeeds after the staged deadline expired; a re-drive that
fails again stays retryable; a crash before the completion replace is finished from `.restored`
without the host. A failed reap completion is still refused `requires recovery`.
