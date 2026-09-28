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
2. If the activation-private staging name exists, whatever it holds (a complete or partial
   target install, or a non-directory), it is removed and the guest synced. No module move has
   happened before `move-ready`/`old-aside`, and nothing else creates that name, so discarding it
   never touches published modules.
3. It then requires the exact source layout (live = recorded source modules, no staging, no old
   name); any other layout refuses without further mutation.
4. It records `module-restored` with the observed live modules.

The existing `define_source` and `restore_power` steps then take the activation to `recovered`,
restoring recorded prior power. Cleanup, the tombstone, and System teardown follow unchanged. One
route serves ordinary recover and teardown.

The authority service's provider-boundary warning always names the exception type. It adds the
message only for a `ProviderRecoveryRefusal`, the kdive-raised recovery and binding refusal whose
text is one of a closed set of literals and so never carries provider output (operator decision,
2026-09-28). Every other adapter or foreign exception is logged by type alone at every level,
keeping ADR-0584's rule that provider output stays out of the operator trail. The refusal text
still passes URL-userinfo redaction and a 512-character bound as defence in depth.

## Consequences

- A teardown of such an activation restarts a prior-running source domain and waits for readiness
  before destroying it, as teardown from every other recoverable phase already does.
- An interrupted install or an interrupted removal leaves only the staging name to discard, so
  recovery converges on retry; `activate` still refuses such a staging name, so recovery is the
  way out of it.
- A crash after `module-restored` re-enters the existing recovery path, whose terminal layout
  check returns early.

## Considered & rejected

- **Widen the partial abort to classify complete, unpublished metadata as abortable.** verified:
  `RecoveryMetadataStore.inspect_abortable_partial`
  (`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py:3876`, commit `e09cf81dc`)
  returns `not-partial` once the complete recovery directory exists, so today the route destroys
  nothing. judgment: widening it cannot remove a staged guest tree without opening the guest,
  bypasses the payload cleanup and tombstone the adapter accounts for complete metadata, and
  leaves ordinary recover of the phase broken.
- **A teardown-only path that skips recovery and destroys directly.** judgment: a second
  destructive route that bypasses the cleanup tombstone the authority accounts for, and leaves
  ordinary recover of the same phase broken.
- **Remove the staging name only when it holds exactly the target manifest.** judgment: refuses
  the states an interrupted install or removal leaves, which then have no supported way out.
- **Skip power restoration for teardown.** judgment: a teardown-specific fork of `recover` for a
  cost every other recoverable phase already pays.
- **Log a redacted, bounded message for every provider exception.** verified: `just ci` on this
  branch failed
  `tests/adversarial/test_external_boot_authority_journal.py::test_recovery_ownership_rejects_drift_without_provider_or_journal_access`,
  which forbids adapter output in any log record. judgment (operator): that contract stands.
- **Log the message only at DEBUG.** judgment (operator): an adapter message must not reach any
  log level.
- **Do nothing.** verified: issue #2880 records the retained live fixture failing teardown with
  `provider_conflict`; a read-only read of that fixture's recovery metadata on 2026-09-28 found
  phase `pre-stop-intent`.
