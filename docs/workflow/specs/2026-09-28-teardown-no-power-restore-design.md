# Teardown without power restore — design (#2898, PR-A)

## Problem

Teardown settles a retained activation through `recover()` or the partial abort; both end in
a power restore (`restore_power()` starts a domain whose metadata says `prior_power ==
"running"`; `_abort_preparation()` calls `session.restore_power()`), booting the guest teardown
destroys. Payload cleanup unlinks payloads in the activation root, but they live in
`<activation>/<digest>/<name>`; the digest `rmdir` fails `ENOTEMPTY` and retries refuse as residue.

## Scope

- `LocalLibvirtExternalBoot.recover()` gains keyword `restore_power: bool = True`. With
  `False`, a `source-restored` point records `recovered` directly and never calls
  `operation.restore_power`. `define_source` already proved source XML and an inactive domain
  before recording `source-restored`; a domain found running later stays running and teardown
  destroys it. The `ExternalBootPorts` and `LocalExternalBootOperation` protocols are unchanged.
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
3. Accepted failure classes: an owned `.bundle.verify`/`.next` temporary left in a committed
   digest directory by a killed materialize retry is refused, as before; the operator bounded
   tolerated residue to exactly `PAYLOAD_NAMES`, so widening it is a follow-up candidate.
4. Covered elsewhere: journal head-mismatch crash (#2899); exhausted-job recycle (#2889);
   generation churn on a deterministic provider conflict (orchestrator-filed churn issue);
   remote-libvirt parity (out of scope).

## Success

- Teardown recover in each resumable phase, teardown partial abort, and a fresh teardown
  mutation call no start seam.
- Default recover and activation-level abort still restore power.
- Cleanup converges from: payloads partly unlinked, projection gone, digest dir gone, archive
  present or absent, and the retained state (digest dir holds `kernel`+`modules`, no projection).
- A foreign file or a symlink payload in the digest dir refuses before any unlink.

## Validation

Each entry is `focused-test`; its red condition is the controlled fault.
- Teardown recover (`test_external_boot_authority.py`): red if `restore-power` is recorded.
- Fresh teardown, recovery absent: red if any start/power action is recorded.
- Coordinator default versus `restore_power=False` (`test_external_boot.py`): red if either
  branch calls the other's seam.
- Aborts: teardown abort red on `power`/`readiness`; activation abort red without them.
- Cleanup states and residue refusal (`test_session_mechanisms.py`): red if unlinks target the
  activation root or the allowlist admits a foreign name or symlink.
- Live settle: `task-test-not-applicable` — needs the lab host (orchestrator-gated).
