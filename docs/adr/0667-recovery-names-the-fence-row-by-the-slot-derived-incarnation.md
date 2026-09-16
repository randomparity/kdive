# 0667 — Recovery names the fence row by the slot-derived incarnation

## Status

Accepted (2026-09-16)

## Context

[ADR-0657](0657-a-successor-invocation-is-terminal-evidence.md) permits a `recover` operation to
"clear the on-disk slot facts and release the `worker_incarnations` fence for a slot proven dead",
and forbids it to "fabricate a `TerminationOutcome`, attribute one invocation's exit facts to
another, or run for a slot whose invocation identity is unreadable". #2532 shipped `recover` for
the case where the retained `state.json` is readable and its binding still matches the row. #2533
covers the residual slots, and two of them have no readable `state.json` at all.

Every write against the fence is keyed on an exact triple — `incarnation`, `authority_kind`,
`authority_binding` — which `public.terminate_worker_incarnation` compares with `=` before it will
mark a row terminated. Today the coordinator gets all three from the retained `SlotState`. With no
readable `state.json` it has none of them, and `authority_store.py` exposes no select-by-slot: the
one read of a stored binding, `authenticate_worker_incarnation`, is keyed on the SHA-256 of the
worker's own credential, and `worker-incarnation.credential` is one of the files
`cleanup_terminated` unlinks. On a wedged slot it is usually already gone.

The fence table is also unreachable directly: `0104_worker_fence_roles.sql` revokes every
privilege on `public.worker_incarnations` from `kdive_lifecycle_witness` along with the other
runtime roles, so all access is through `SECURITY DEFINER` functions.

## Decision

**Recovery names the row by the slot-derived incarnation prefix, and releases the fence with the
row's own stored binding.**

`SlotState.incarnation` is not free-form. Its model validator requires
`local-systemd:kdive-live-worker@{slot}.service:{generation}`, so the only part a lost
`state.json` takes with it is the generation. The slot number is fixed and always known to the
coordinator. A new `SECURITY DEFINER` **read** function returns the `active` rows whose
`incarnation` carries that exact slot-derived prefix, with their stored `authority_binding`.

The binding that comes back is then passed **unchanged** into the existing
`public.terminate_worker_incarnation`. The exact-equality the fence demands is satisfied by
construction, so the fence protocol, the terminate function, and the `worker_incarnations` table
are all unchanged and `CURRENT_WORKER_FENCE_PROTOCOL` stays 4.

**Death is proven against the row's identity, not the slot's.** The stored binding carries the
`boot_id` and `invocation_id` the fence itself claims are running, and the outcome is derived from
a current systemd observation against that pair by the same rules ADR-0657 already set. Nothing is
synthesized, and no invocation's exit facts are attributed to another. This also settles the
drifted-binding case correctly rather than incidentally: where the retained `state.json` and the
row disagree, the row is the fence holder, so it is the row's identity that has to be proven dead.

**What replaces the binding match as the safety property is a liveness refusal.** The exact-binding
comparison protects the fence from a caller quoting stale facts. Recovery cannot offer that proof,
so it earns the release a different way: it refuses any slot whose unit cgroup is populated, and
refuses any slot it cannot prove dead. A slot holding a fence whose invocation identity is
unreadable is refused with its own disposition, per ADR-0657.

## Consequences

An operator recovering a slot no longer needs a hand-run `UPDATE` against `worker_incarnations`
(#2481). The release is reached through the contract, for all four recoverable residual cases.

The read function returns a binding for an `active` row, which is a fact the witness role already
holds for every worker it registers and which `authenticate_worker_incarnation` already returns.
It carries no credential and no envelope.

A slot can hold more than one `active` row: if its files are lost out of band, `prepare` mints a
fresh generation and registers a second row while the first stays active. The read function
therefore returns a bounded set rather than one row, and recovery retires each row it can prove
dead. A slot with a row it cannot prove dead is refused as a whole, so a partially released slot
is not reported as recovered.

Recovery now reaches the fence for a slot whose `state.json` is absent, where before it cleared
only the failed unit identity. The two `recover` tests #2532 wrote to pin that residual behaviour
change with it; both name #2533 in their own docstrings as the issue that would.

## Considered & rejected

- **Add a recovery-specific terminate that skips the binding comparison.** judgment: it puts the
  fence's only exact-match guard behind a second door, and every later reader has to establish
  which callers use which door. Echoing the stored binding needs no new write path at all.
- **Widen `public.terminate_worker_incarnation` to accept a NULL binding as "any".** verified: the
  same function body serves `stop`'s normal path — `0110_idempotent_worker_termination.sql` is the
  sole definition and `PostgresAuthority.terminate` its only caller — so the relaxation would
  apply to the ordinary path too, where the binding match is the whole point.
- **Key the lookup on the credential hash, as `authenticate_worker_incarnation` does.**
  verified: `worker-incarnation.credential` is unlinked by `cleanup_terminated`
  (`systemd_worker_state.py`, alongside `worker.env` and `release`), so on the wedged slots this
  record exists for, the credential is the file most likely already gone.
- **Store the generation in a second file beside `state.json` so it survives the parse failure.**
  judgment: a second on-disk copy of a derived fact has to be kept honest by something, and the
  case it addresses is exactly the one where on-disk facts are not trustworthy. The database
  already holds the authoritative copy.
- **Add a `slot` column to `worker_incarnations` and select on it.** verified: epic #2484's
  non-goals exclude a `worker_incarnations` schema change; the operator admitted that exclusion
  for #2533 on 2026-09-16, but the prefix is exactly as selective and needs no column, so the
  admission is left unspent.
- **Do nothing and keep the documented manual `UPDATE`.** verified: #2481 is the original report
  that the manual procedure is the defect, and #2488 is the epic that exists to remove it.
