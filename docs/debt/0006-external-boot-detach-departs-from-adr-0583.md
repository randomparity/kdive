# 0006 — The shipped matrix admits external-boot debug detach where ADR-0583 rejects it

## Status

Open
review-by: 2027-03-02

## Concern

ADR-0583:348-350 states that `preparing`, `prepared`, `activating`, `recovering`,
`recovery_conflict`, `recovery_failed`, and the uncleaned terminal states admit only
activation-owned continuation, reconciliation, conflict resolution, or authorized teardown,
and that "every new install, lifecycle, power/control, snapshot, capture, and debug
operation is rejected". ADR-0583:351 separately scopes debug attach and detach to the Run
that owns the activation, within `active`.

The matrix this change ships departs from both clauses for detach, and only for detach.
`src/kdive/services/external_boot/admission.py` puts `DEBUG_DETACH` in `_ALWAYS_ADMITTED`,
so it is admitted in every restricting state including the six the first clause rejects it
in, and leaves it out of `_OWNING_RUN_SCOPED`, so it is admitted whichever Run owns the
session. `DEBUG_ATTACH` keeps both fences: `active` only, owning Run only.

The departure is deliberate and has two reasons, both about state that would otherwise be
unreachable rather than about convenience.

**A denied detach strands what it refuses to clean.** A detach reverses an attach the matrix
itself admitted. `src/kdive/services/debug/lifecycle.py` closes the provider transport and
writes `DebugSessionState.DETACHED` in the same locked block; denying the call leaves the
session row `live` and its gdbstub or drgn transport open on the provider host, with no
action the agent holding the session can reach. Denying it protects nothing — the attach has
already happened — and costs a leaked transport.

**A fenced detach wedges the release it was meant to unblock.**
`src/kdive/mcp/tools/external_boot/recovery_requests.py` refuses
`runs.release_external_boot` with `reason=debug_session_active` whenever
`active_session_ids_for_system` (`src/kdive/services/debug/sessions.py`) returns anything,
and that helper joins `debug_sessions` to `runs` on `runs.system_id` — it spans every Run
ever bound to the System, by design. Fencing `DEBUG_DETACH` to the activation's Run makes
those two halves disagree: a live session owned by a different Run of the same System blocks
the release and can be detached by nobody, because the detach guard compares the session
row's own `run_id` against the activation's. The owning contributor's remaining exits are
`systems.teardown` (project `ADMIN`, and filtered out of a contributor's suggested next
actions) or the reconciler's stale-heartbeat reaper, which never fires while the other Run's
worker is alive. Widening the detach is the cheaper of the two ways to make the halves agree,
and it is the one that does not narrow a refusal the ADR asks for.

What is recorded here is not the behaviour — it is that ADR-0583 is the durable record and it
still describes a matrix the code does not implement. Whoever builds #2118's activation
lifecycle reads the ADR, and on detach the ADR is wrong about the running system.

## Why deferred

Reconciling the text means amending or superseding an accepted ADR, and #2117's charter
assigns no ADR number to this change; the design artifact
(`docs/workflow/specs/2026-08-29-external-boot-admission-agent-contracts-design.md`) states
that explicitly. An amendment written here would also be written blind: the recovery states
the first clause covers have no implementation on this branch, so the detach behaviour in
`recovering` and `recovery_conflict` cannot be exercised against a real activation, and the
right ADR text depends on how #2118 drives those states.

The alternative reading — narrow the release's `debug_session_active` refusal to the owning
Run instead, and keep detach fenced — was rejected rather than deferred. It would let a
release proceed with a live foreign debug session attached to a System whose kernel is about
to be swapped, which is the interleaving the refusal exists to stop.

## Non-regression boundary

- `DEBUG_ATTACH` must stay admitted in `active` only and owning-Run scoped. The widening
  recorded here covers detach and nothing else;
  `tests/services/external_boot/test_admission.py` asserts the whole table transposed, so
  moving `DEBUG_ATTACH` fails there.
- The release's `debug_session_active` refusal must stay System-wide, regardless of owning
  Run, while this record is open — narrowing it is the rejected alternative above, and
  `tests/services/external_boot/test_recovery_requests.py` covers the refusal.
- A detach must remain reachable by a non-owning Run in a restricting state;
  `tests/services/external_boot/test_reverse_admission.py` covers that directly.
- `src/kdive/services/external_boot/admission.py` carries a comment naming this record beside
  `_ALWAYS_ADMITTED`, so a reader of the guard is not left to infer that ADR-0583 was
  transcribed faithfully.

## What would resolve it

Amend ADR-0583, or supersede it, so the durable record states the carve-out the code
implements: detach is admitted in every restricting state and for any Run of the System,
because it is the reversal of an admitted attach and because the release's blocking condition
is System-wide. The amendment belongs with #2118, which drives the recovery states the first
clause covers and can exercise the behaviour it describes.

Done when ADR-0583's restricted-state and owning-Run clauses read the same as
`src/kdive/services/external_boot/admission.py`, with the reasoning recorded in the ADR rather
than only here, and this record carries its resolution banner.

## Provenance

target: src/kdive/services/external_boot/admission.py
target: src/kdive/mcp/tools/external_boot/recovery_requests.py
Found by the `$gauntlet` adversarial review of the #2117 branch on 2026-09-02 (findings 4 and
6 of 8), dispositioned `accepted-fixed` for the code half — `DEBUG_DETACH` was removed from
the owning-Run-scoped set in that fix — and deferred for the ADR half, which is this record.
tracker: #2118
