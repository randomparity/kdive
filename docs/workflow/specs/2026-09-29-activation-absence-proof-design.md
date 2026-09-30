# Non-System partial abort proves recovery absence (#2926)

## Scope and authority

Campaign scope for issue #2926, token `q2926-96f3c17b`. Operator-approved exclusions
(2026-09-29): the System-teardown path (#2898, already fixed); remote-libvirt parity (operator);
generation churn caused by the resulting conflict (#2901). Decision record:
[ADR-0710](../../adr/0710-local-external-boot-session-opens-its-artifact-root-on-first-use.md),
recorded against ADR-0587 as a dated amendment.

## Problem

`LocalExternalBootSessionFactory.open` (`lifecycle/boot/session.py`) calls
`open_artifact_root(facts)` during construction, bound in production to `LocalArtifactRoot.open`,
which creates `<system>/<run>/<activation>` under the recovery root. The authority's non-System
TEARDOWN with no recovery point runs, through `LocalLibvirtExternalBoot` ports that each open a
session: `abort_preparation` → `recovery_is_absent` → `observe_state` (`_absent_observation`); a
later `_observe_recovery` runs `recovery_is_absent`, or `recovery_point` → `cleanup_receipt` →
`recovery_is_absent`. Each open re-creates the activation directory, and
`RecoveryMetadataStore.exact_recovery_absence` is false while it exists, so the proof never holds
and the authority raises `provider_conflict`.

## Design

1. **Lazy artifact root (root cause).** The factory passes `_ConcreteSession` a zero-argument
   opener bound to the pinned `facts` snapshot instead of an open descriptor.
   `_ConcreteSession._artifact_root()` opens it once under `_lifecycle_lock`, after
   `_require_open_domain()`, and caches the descriptor. Every current `self._artifact_fd` consumer
   (`projection_directory`, `reopen_projection`, `projection_artifact_path`, `open_artifact`,
   `open_projection_artifact`, `unlink_artifact`, `cleanup_payloads`, `_download_artifact`) calls
   it. `close()` closes the descriptor only when it was opened; the close order is unchanged. The
   factory's failure cleanup no longer handles an artifact descriptor.
2. **Prune after non-System abort.** `_RealLocalExternalBootOperation.abort_preparation` calls
   `store.prune_empty_activation_parents(binding)` when the result is `removed` or `absent`,
   exactly as `abort_system_teardown_preparation` does. This converges directories created before
   the change or by a materialization interrupted before its receipt.
3. Docstrings that say session open creates the directories (`LocalArtifactRoot`,
   `prune_empty_activation_parents`) are corrected.

No change to `external_boot_authority.py`, `composition.py`, the `OpenArtifactRoot` type, the
on-disk layout, or any persisted record.

## Success

- S1: opening and closing a real session bound to `LocalArtifactRoot` creates nothing under the
  recovery root; the first artifact use creates the chain once and later uses reuse it.
- S2: with real session factory, `LocalArtifactRoot`, `RealLocalExternalBootIO` and
  `LocalLibvirtExternalBoot`, the port sequence the authority runs for a non-System TEARDOWN with
  no point (listed in Problem, both `_observe_recovery` branches) returns `True` from every
  `recovery_is_absent` and leaves no `<system>` directory, for four start states: nothing
  (`absent`); an abort receipt left by an interrupted earlier abort (`removed`); an empty complete
  recovery directory (#2927 interrupted rmdir, `absent`); pre-existing empty activation parents
  (`absent`).
- S3: a file inside the activation directory keeps `recovery_is_absent` false and is not removed.
- S4: existing write-path tests (materialize, transfers, cleanup) stay green.

## Failure model

1. **Actors and deployments**
   - The local external-boot authority on a local-libvirt worker host, driven by the job runner.
2. **Invariants and assets at stake**
   - Absence is proven only when no activation-owned storage exists (persisted recovery state).
   - Pruning never removes a non-empty directory (quarantine evidence).
   - The artifact root is opened only under the pinned lease and its frozen ownership snapshot
     (operation-lease lane).
3. **Accepted failure classes**
   - A malformed activation directory is refused at first use instead of at session open; ADR-0710
     Consequences states the one ordering change (`prepare` after its clean stop) and why recovery
     already covers it.
   - Residue in the activation directory keeps absence false and ends in `provider_conflict`: that
     is the quarantine the proof exists for.
4. **Covered elsewhere**
   - System-teardown path: #2898. Remote libvirt: operator. Generation churn: #2901.
