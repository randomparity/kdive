# 0667 — Recovery names the fence row by the slot-derived incarnation

## Status

Accepted (2026-09-16)

## Context

[ADR-0657](0657-a-successor-invocation-is-terminal-evidence.md) lets a `recover` operation clear
the on-disk slot facts and release the `worker_incarnations` fence for a slot proven dead, and
forbids it to fabricate a `TerminationOutcome`, attribute one invocation's exit facts to another,
or run for a slot whose invocation identity is unreadable. #2532 shipped `recover` for the case
where the retained `state.json` is readable and its binding still matches the row. #2533 covers the
residual slots, and two of them have no readable `state.json` at all.

Every write against the fence is keyed on an exact triple — `incarnation`, `authority_kind`,
`authority_binding` — which `public.terminate_worker_incarnation` compares with `=` before marking
a row terminated. The coordinator gets all three from the retained `SlotState`. With no readable
`state.json` it has none of them, and `authority_store.py` exposes no select-by-slot: the one read
of a stored binding, `authenticate_worker_incarnation`, is keyed on the SHA-256 of the worker's own
credential, and `worker-incarnation.credential` is one of the files `cleanup_terminated` unlinks.
On a wedged slot it is usually already gone.

The table is also unreachable directly: `0104_worker_fence_roles.sql` revokes every privilege on
`public.worker_incarnations` from `kdive_lifecycle_witness` along with the other runtime roles, so
all access is through `SECURITY DEFINER` functions.

## Decision

**Recovery names the row by the slot-derived incarnation prefix, and releases it with the row's own
stored binding.**

`SlotState.incarnation` is not free-form. Its model validator requires
`local-systemd:kdive-live-worker@{slot}.service:{generation}`, so the only part a lost `state.json`
takes with it is the generation; the slot number is fixed and always known. A new `SECURITY
DEFINER` **read** function returns the `active` rows carrying that exact slot-derived prefix, with
their stored `authority_binding`.

That binding is passed **unchanged** into the existing `public.terminate_worker_incarnation`. The
exact equality the fence demands is satisfied by construction, so the fence protocol, the terminate
function and the `worker_incarnations` table are all unchanged and `CURRENT_WORKER_FENCE_PROTOCOL`
stays 4.

**Death is proven against the row's identity, not the slot's.** The stored binding carries the
`boot_id` and `invocation_id` the fence itself claims are running, and the outcome is derived from a
current systemd observation against that pair by the rules ADR-0657 already set. Nothing is
synthesized, and no invocation's exit facts are attributed to another. This also settles the
drifted-binding case on the merits: where the retained `state.json` and the row disagree, the row is
the fence holder, so it is the row's identity that has to be proven dead.

**What replaces the binding match as the safety property is a liveness refusal.** The exact-binding
comparison protects the fence from a caller quoting stale facts. Recovery cannot offer that proof,
so it earns the release differently: it refuses any slot whose unit cgroup is populated, and any
slot it cannot prove dead. Two refusals are distinct per-slot dispositions rather than sweep-ending
errors, so one bad slot never hides the seven the call could still retire.

**A slot whose invocation identity is unreadable is refused, and that case is a same-boot absence.**
ADR-0574 holds that absence within the retained boot is never termination evidence, so when systemd
reports no invocation for the unit on that same boot there is nothing that can prove the registered
invocation ended. That is the unreadable identity ADR-0657 names, and recovery refuses it with its
own greppable disposition instead of clearing it. It is a terminal state by design: the operator's
remedy is a reboot, which yields a different `boot_id` and therefore real evidence.

## Consequences

An operator recovering a slot no longer needs a hand-run `UPDATE` against `worker_incarnations`
(#2481). The release is reached through the contract for all four recoverable residual cases.

The read function returns a binding for an `active` row — a fact the witness role already holds for
every worker it registers, and which `authenticate_worker_incarnation` already returns. It carries
no credential and no envelope.

**The prefix does not include a host, so the caller filters on the binding's `host`.** The
incarnation names only the unit and generation, so two hosts sharing one database could in
principle both match a slot's prefix. Generations are 128-bit random, making a collision
negligible, but the fence is not something to protect by improbability: recovery skips any row
whose stored `host` is not this host's. That makes the single-host premise explicit rather than
load-bearing and unstated.

A slot can hold more than one `active` row: if its files are lost out of band, `prepare` mints a
fresh generation and registers a second row while the first stays active. The read function
therefore returns a bounded set. Recovery classifies **every** row before releasing any of them, so
a slot with one unrecoverable row is refused whole rather than left half-released.

Recovery now reaches the fence for a slot whose `state.json` is absent, where before it cleared only
the failed unit identity, and it reports a same-boot absence as its own refusal rather than as a
systemd outage. Three `recover` tests #2532 wrote to pin the residual behaviour change with it.

### Amendment (2026-09-17): the implementation was reverted; the decision stands unimplemented (#2533)

This is an amendment rather than a rewrite because the decision above was not withdrawn or
reconsidered — it was implemented, merged, and then taken back for a reason that has nothing to do
with its merits. The original record is preserved; what follows qualifies every operator-facing
claim in this section.

PR #2592 implemented this decision and merged on 2026-09-16. PR #2597 reverted that merge on
2026-09-17, because #2592 was merged without the authority to do so — this is a `risk:daytime-only`
row that was to await an operator decision, and the merge was taken unattended with a guard that
omitted the ancestry check. Nothing was wrong with the change itself: it met all seven of its
acceptance criteria and `main` was green with it.

**So the two claims above describe the intended end state, not `main` today.** Recovery does *not*
yet reach the fence for a slot whose `state.json` is absent, and an operator who hits one **has no
sanctioned remedy**. The hand-run `UPDATE` this section says they no longer need was not restored as
guidance by the revert, and it is not guidance to return to: the operator documentation carries no
such procedure — `docs/operating/` mentions the `worker_incarnations` row only to forbid editing it
— and `## Considered & rejected` below records #2481 as the report that the manual procedure is
itself the defect. `docs/operating/runbooks/live-stack.md` is the accurate document
while this amendment stands — those cases stay wedged after a `recover` pass, and the instruction
not to hand-edit slot files or the `worker_incarnations` row remains in force. The slot stays wedged
until #2533 lands.

One artifact of this decision survives the revert and will not be re-added.
`src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql` remains on `main` as an inert
read-only function with no callers, because ADR-0015 makes applied migrations forward-only and
byte-immutable and the schema guard rejects a delete. A second attempt at #2533 builds on that
migration rather than introducing one; if this decision is abandoned instead, retiring the function
is itself a forward migration.

Status is deliberately left at **Accepted**. A partially-shipped ADR would ordinarily return to
**Proposed**, but the retained migration cites `ADR-0667` in `src/`, and `adr-status-check` rejects a
Proposed ADR cited from that tree — so the flip that would describe reality more accurately is the
one the guards forbid. This amendment carries that meaning instead.

### Amendment (2026-09-19): the implementation is restored (#2533)

This amendment supersedes only the implementation-status qualification in the 2026-09-17
amendment. The accepted decision and the history of its first merge and revert remain unchanged.

The reimplementation uses the retained migration and restores recovery for all four recoverable
residual cases: absent or malformed `state.json`, a drifted stored binding, and rejected ordinary
termination evidence. A provisioned systemd host verified each case clears the four fixed slot
facts and leaves no active applicable fence; a live unit refuses recovery before mutation for
every residual shape. Case 5 remains an explicit refusal: unreadable retained identity without
proof of a different boot returns `recovery_refused_unreadable_identity`, preserving the slot
files and fence. No manual edit of the slot files or `worker_incarnations` is required or
sanctioned. The operator runbook now describes this restored behavior.

## Considered & rejected

- **Add a recovery-specific terminate that skips the binding comparison.** judgment: it puts the
  fence's only exact-match guard behind a second door, and every later reader has to establish which
  callers use which. Echoing the stored binding needs no new write path at all.
- **Widen `public.terminate_worker_incarnation` to accept a NULL binding as "any".** verified: the
  four-argument form has three production callers —
  `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py:171`,
  `src/kdive/processes/lifecycle/lifecycle_witness.py:124` and
  `src/kdive/processes/lifecycle/compose/compose_worker_lifecycle.py:378` (`rg -n
  terminate_worker_incarnation src/ -g '*.py'`, this branch at dc5592f06) — so the relaxation would
  reach `stop`'s ordinary path and the compose tier too, where the binding match is the whole point.
- **Key the lookup on the credential hash, as `authenticate_worker_incarnation` does.** verified:
  `worker-incarnation.credential` is unlinked by `cleanup_terminated`
  (`systemd_worker_state.py`, alongside `worker.env` and `release`), so on the wedged slots this
  record exists for, the credential is the file most likely already gone.
- **Store the generation in a second file beside `state.json` so it survives the parse failure.**
  judgment: a second on-disk copy of a derived fact needs something to keep it honest, and the case
  it addresses is exactly the one where on-disk facts are not trustworthy. The database already
  holds the authoritative copy.
- **Add a `slot` column to `worker_incarnations` and select on it.** verified: epic #2484's
  non-goals exclude a `worker_incarnations` schema change; the operator admitted that exclusion for
  #2533 on 2026-09-16, but the prefix is exactly as selective and needs no column, so the admission
  is left unspent.
- **Do nothing and keep the documented manual `UPDATE`.** verified: #2481 is the original report
  that the manual procedure is itself the defect, and #2488 is the epic that exists to remove it.
