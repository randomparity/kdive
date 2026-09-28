# Pre-target teardown recovery and provider-boundary diagnostics (#2880)

## Scope and authority

Campaign scope for issue #2880, token `q2880-8209b650`. Operator-approved exclusions
(2026-09-28): the activate-time accelerator refusal (#2867 / #2877); the fail-commit fence and
superseded diagnostics (#2881); executor threading (#2878, merged); the KVM group refresh (#2877);
manual database or journal edits and host reset (not authorized). The recovery transition is
recorded in [ADR-0707](../../adr/0707-local-recovery-settles-pre-stop-intent.md).

## Problem

Authority System teardown of a local activation whose `activate` failed before publishing any
module move calls `recover()` on metadata at `pre-stop-intent`, which raises
`ValueError("external-boot recovery phase is not resumable")`. The authority service converts that
to `provider_conflict` and logs only `authority provider boundary failed` with no exception type
or message, so the operator cannot see which step failed. The retained lab fixture's metadata
reads `pre-stop-intent` with prior power `running` (read-only check, 2026-09-28).

## Design

**Recovery** (`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`, ADR-0707).
`LocalLibvirtExternalBoot.recover` adds `pre-stop-intent` to its resumable set.
`RealLocalExternalBootOperation.recover_modules` (the concrete operation) branches on that phase
before `_stop_for_recovery` into a new private `_settle_unpublished_modules(metadata)`:

- require `_host_state(metadata) == ("source", False)`, else `ValueError`; then
  `self._session.require_inactive()`;
- open the guest, build `_SessionModulePublicationIO`, observe the layout;
- layout `ModuleLayout(prior, None, None)`: no guest change; layout
  `ModuleLayout(prior, desired, None)`: `publication.remove_staging()` (new: `rm_rf` of the
  staging name), `guest_sync()`, re-observe and require `ModuleLayout(prior, None, None)`; any
  other layout: `ValueError` before mutation;
- `record_phase(metadata, "module-restored", inactive_modules=<observed live modules>)`.

`prior` and `desired` are computed exactly as `activate_modules` computes them. The coordinator
then runs the existing `define_source` and `restore_power`. The authority adapter is unchanged:
its existing `recover` → cleanup → tombstone → `teardown_system` sequence now succeeds.

**Diagnostics** (`src/kdive/providers/external_boot_authority/service.py`). `_provider_error`
takes an optional `error: Exception | None`. When given, the warning becomes
`authority provider boundary failed: %s: %s` with the exception's `type(error).__qualname__` and
a message produced by `redact_url_credentials`, then a module-level
`Redactor(registry=SecretRegistry())`, truncated to 512 characters; the same two values are added
to `extra` as `error_type` and `error_message`. The message is in the formatted text because the
authority host installs no JSON formatter, so `extra` alone is invisible there. Every
`except Exception:` site that returns `_provider_error` binds and passes the exception; the wire
error and `from None` are unchanged. The two sites with no exception keep the old line.

## Success

1. `recover` on complete metadata at `pre-stop-intent`, inactive source XML, and the source layout
   reaches `recovered` with no guest module mutation, restoring prior power `running`.
2. The same with target modules staged removes only the staging name, then reaches `recovered`;
   an interruption right after that removal converges to `recovered` on retry.
3. At `pre-stop-intent`, an active domain, a non-source XML, or a layout other than the two above
   raises `ValueError` with the phase still `pre-stop-intent` and no stop, define, start, or
   guest removal recorded.
4. Authority-adapter System teardown on `pre-stop-intent` metadata reaches complete teardown facts
   with recover, cleanup, tombstone finalization, and host teardown in that order.
5. A provider adapter raising a non-`AuthorityServiceError` yields wire `provider_conflict` and one
   warning whose text names the exception type and the message with a `password=` value and URL
   userinfo password redacted, bounded to 512 characters.
6. Live, on the retained fixture: terminal teardown state, domain absent, cleanup evidence
   recorded, the 32 GiB ready reservation credited exactly once, and a retry that adds no second
   credit; no manual DB/journal edit or host reset. This also proves #2881's share of #2878
   criterion 6. It runs only after #2881 merges.

## Failure model

- **Actors and deployments:** the provider-authority host process on operator lab hosts; the jobs
  worker that submits authority teardown; operators reading the authority host's stderr/journal.
- **Invariants and assets:** a guest module tree changes only by removing this activation's
  private staging name; the domain is never stopped or redefined from `pre-stop-intent`; one
  tombstone and one reservation credit per activation; no registered secret or credential-shaped
  value reaches the log.
- **Accepted failure classes:** a partially installed staging name still refuses (ADR-0707,
  unchanged from `activate`); a prior-running source that fails readiness fails teardown with the
  now-logged cause, as for other phases; the log message may carry host paths from `OSError`
  text, accepted because the authority log is operator-private and the paths are the diagnosis.
- **Covered elsewhere:** fail-commit fence and superseded diagnostics (#2881); accelerator refusal
  at activate (#2867 / #2877); remote-provider recovery (unchanged, not local).

### Threat model

- **Boundary:** exception text crossing from provider code into the operator log (widened: it was
  dropped before). No new entry point or wire field.
- **Actor:** an operator with log access; exception text may embed a DSN or `key=value` secret.
- **Control:** URL-userinfo and key/value redaction plus a length bound, applied before logging;
  the handler-level `SecretRedactionFilter` still applies where `configure_logging` ran.
- **Out of scope:** secrets with no key/value or URL shape, since the authority host holds no
  secret registry; accepted with the log's operator-private scope.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Criteria 1–3 | focused-test | new cases in `tests/providers/local_libvirt/test_external_boot.py` via `_restart_fixture`; red on `main` with `not resumable` |
| Criterion 4 | focused-test | new case in `tests/providers/local_libvirt/test_external_boot_authority.py` with `_FakeIO(_metadata("pre-stop-intent"))`; red on `main` |
| Criterion 5 | focused-test | new case in `tests/providers/external_boot_authority/test_service_teardown.py` with `caplog`; red on `main` (no type in message) |
| Criterion 6 | task-test-not-applicable | needs the operator host's retained fixture; evidence recorded in the PR |
