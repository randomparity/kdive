# 0707 — Local external-boot recovery settles an unpublished pre-stop-intent activation

## Status

Accepted (2026-09-28)

## Context

Local preparation stops the domain and then writes complete recovery metadata at phase
`pre-stop-intent`. `activate` resumes from that phase, but `recover` rejects it with
`external-boot recovery phase is not resumable`. An activation whose `activate` failed before
publishing any module move (for example the accelerator refusal in the session `open()`) is
therefore unrecoverable: ordinary recover fails, and authority System teardown, which recovers,
cleans up, and tombstones a recovery point before destroying the domain, fails the same way
(#2880). The partial-preparation abort of
[ADR-0602](0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md) does not apply
because complete metadata exists.

At `pre-stop-intent` nothing target-side is live: the domain carries its recorded source XML and
is inactive, and the guest module tree is the source layout. `activate` may have created the
activation-private staging name `/lib/modules/.kdive-<activation>-staging` and installed the target
modules into it before recording `move-ready` or `old-aside`.

## Decision

`recover` accepts complete metadata at `pre-stop-intent`. The local operation's `recover_modules`
handles that phase itself:

1. It requires the exact recorded source XML with the domain inactive and stops nothing; any other
   host state refuses without mutation.
2. It observes the three-name module layout. The exact source layout needs no guest change. The
   source layout plus a staging name holding exactly the target manifest has that staging name
   removed and synced, then the layout is re-observed. Any other layout refuses without mutation.
3. It records `module-restored` with the observed live modules.

The existing `define_source` and `restore_power` steps then take the activation to `recovered`,
restoring recorded prior power. Cleanup, the tombstone, and System teardown follow unchanged. One
route serves ordinary recover and teardown.

## Consequences

- A teardown of such an activation restarts a prior-running source domain and waits for readiness
  before destroying it, as teardown from every other recoverable phase already does.
- A partially installed staging name (install interrupted before the layout matched) is still
  refused, exactly as `activate` refuses it; the activation stays retained for an operator.
- A crash after the staging removal re-enters at `pre-stop-intent` and sees the plain source
  layout; a crash after `module-restored` re-enters the existing recovery path, whose terminal
  layout check returns early.

## Considered & rejected

- **Route `pre-stop-intent` teardown through the partial-preparation abort.** verified:
  `RecoveryMetadataStore.inspect_abortable_partial`
  (`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py:3876`, commit `e09cf81dc`)
  returns `not-partial` once the complete recovery directory exists, so
  `_prepare_system_teardown_recovery` returns without destroying the domain.
- **A teardown-only path that skips recovery and destroys directly.** judgment: a second
  destructive route that bypasses the cleanup tombstone the authority accounts for, and leaves
  ordinary recover of the same phase broken.
- **Skip power restoration for teardown.** judgment: a teardown-specific fork of `recover` for a
  cost every other recoverable phase already pays.
- **Do nothing.** verified: issue #2880 records the retained live fixture failing teardown with
  `provider_conflict` at this phase; the fixture's metadata reads `pre-stop-intent`.
