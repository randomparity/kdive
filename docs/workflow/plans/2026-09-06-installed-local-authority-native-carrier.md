# Installed local-authority native carrier

**Scope.** Issue #2151's x86_64 local-libvirt arm only. The carrier exercises one installed,
coherent KDIVE revision through its active fixed worker and authority socket. It never provisions,
reboots, or performs host-wide cleanup. Remote-libvirt and native ppc64le remain outside this plan.

## Contracts

- Unset carrier configuration skips. Once its trigger is set, a missing companion, revision skew,
  inactive worker incarnation, unsafe proof file, or unavailable authority fails before mutation.
- A mode-`0600` operator file supplies only references and IDs: expected installed SHA, project,
  authority service name, one pre-provisioned disposable System, and a unique ownership prefix.
  Credential contents are never accepted by the pytest process.
- The carrier records every System, Run, activation, domain, volume, object key, journal lane, and
  recovery path it creates before use. Cleanup targets only that exact ledger. Unknown or
  pre-existing resources make cleanup fail closed; no prefix-wide or host-wide reaper is allowed.
- The six job operations are activate, recover, resolve-conflict, release, cleanup, and teardown.
  They travel through the running MCP/worker route and poll durable jobs; direct provider calls and
  fake ports do not count as native evidence.
- The ordinary activate then release/cleanup chain is independent of the fault arms. Restart,
  journal-loss, unresolved-takeover, and stale-write arms require named deterministic
  barriers after the real provider effect and before terminal persistence. The assembled runtime
  currently exposes no such carrier control. Those arms must fail as unavailable when the native
  trigger is set; unit tests must not mock them green.

## Threat model

The operator proof file crosses into pytest and is constrained to an owner-only regular file with a
closed schema, absolute paths, UUIDs, a full Git SHA, and a bounded ownership prefix. MCP results and
the owned-resource ledger are untrusted until schema validation. Cleanup accepts exact recorded
identifiers only. The authenticated worker and systemd operator are trusted to preserve their
existing credential and service controls. Compromised root, libvirt, PostgreSQL, or S3 services are
out of scope; the carrier detects their observable contract failures but cannot contain them.

## Tasks and verification

1. Add a local-authority live configuration gate and immutable owned-resource ledger helper.
   Red: imports or validation tests fail because neither exists. Green:
   `uv run python -m pytest tests/live_vm/test_installed_local_authority_support.py -q`.
2. Add the marked native carrier. It skips only when the trigger is absent, proves installed SHA
   and active authority/worker health, then drives install, activate, and release through the public
   job path. The Investigation and Run created by the carrier are recorded immediately; the exact
   Investigation is closed after release. Recover, resolve-conflict, and teardown are separate
   state/fault arms. Deterministic fault arms remain fail-loud until the assembled runtime supplies
   barriers; they are never represented by fake helper tests. Green (collection/non-live gate):
   `uv run python -m pytest tests/live_vm/test_installed_local_authority.py -q`.
3. Update the live-testing runbook with the protected config shape, exact focused command, resource
   envelope, cleanup report, and explicit distinction between collected/unit evidence and the first
   real native run. Run `just lint`, `just type`, and the focused tests. Root alone schedules the
   first native mutation and records which arms executed or remained blocked.
