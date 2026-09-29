# Teardown without power restore — design (#2898, PR-A)

## Problem

Local System teardown settles a retained activation through `recover()` or the teardown
partial abort. Both end in a power restore: `restore_power()` starts a domain whose recovery
metadata says `prior_power == "running"`, and `_abort_preparation()` calls
`session.restore_power()`. A teardown therefore boots the guest it is about to destroy. Its
payload cleanup then unlinks `kernel`/`initrd`/`modules` in the activation root, but payloads
live in the projection digest directory (`<activation>/<digest>/<name>`). The digest `rmdir`
fails `ENOTEMPTY`, and every retry refuses with "target projection contains unexpected residue".

## Scope

- `LocalLibvirtExternalBoot.recover()` gains keyword `restore_power: bool = True`. With
  `False`, a `source-restored` point records `recovered` directly and never calls
  `operation.restore_power`. `define_source` already proved source XML and an inactive domain
  before recording `source-restored`; a domain found running later stays running and teardown
  destroys it. `ExternalBootPorts.recover` and the `LocalExternalBootOperation` protocol keep
  their signatures.
- `_prepare_system_teardown_recovery` calls `recover(point, authority, restore_power=False)`.
- `_abort_preparation()` gains keyword `restore_power: bool`: the System-teardown abort passes
  `False`, the activation-level `abort_preparation` passes `True` (today's behaviour).
- `LocalPayloadCleanup.cleanup` validates the digest directory before any unlink: every entry
  is `target-projection.json` or a `PAYLOAD_NAMES` member, each payload is a non-symlink
  regular file, and a present projection file still validates. It then unlinks payloads and
  projection inside the digest directory, removes it, and removes the archive. Absence at
  every step is success. Unit fixtures that place payloads in the activation root move.

### Failure model

1. Actors and deployments: the local provider-authority process running System teardown on
   an operator host; the retained #2867 fixture.
2. Invariants and assets: a teardown never starts a domain; non-teardown recovery keeps
   restoring prior power; cleanup deletes only owned payloads (ADR-0600/0584) and never
   follows a symlink.
3. Accepted failure classes: a stale `.kernel.next`-style temporary in the digest directory is
   refused as residue — refused before this change too (an `rmdir` failure), never deleted.
4. Covered elsewhere: journal head-mismatch crash (#2899); exhausted-job recycle (#2889);
   generation churn on a deterministic provider conflict (orchestrator-filed churn issue);
   remote-libvirt parity (out of scope).

## Success

- Teardown recover in each resumable phase and teardown partial abort call no start seam.
- Default recover and activation-level abort still restore power.
- Cleanup converges from: payloads partly unlinked, projection gone, digest dir gone, archive
  present or absent, and the retained state (digest dir holds `kernel`+`modules`, no projection).
- A foreign file or a symlink payload in the digest dir refuses before any unlink.

## Validation

- Teardown recover: `focused-test`, `test_external_boot_authority.py`, red while
  `restore-power` appears in the fake's actions.
- Coordinator default versus `restore_power=False`: `focused-test`, `test_external_boot.py`.
- Teardown abort: `focused-test`, the teardown partial-abort test expects no `power` or
  `readiness`; the activation abort test keeps both.
- Cleanup convergence and residue refusal: `focused-test`, `test_session_mechanisms.py`,
  parametrized over each partial state plus foreign-file and symlink refusal.
- Live settle: `task-test-not-applicable` — needs the lab host; the orchestrator-gated settle.
