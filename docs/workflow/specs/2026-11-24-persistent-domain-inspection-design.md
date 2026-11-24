# Persistent domain inspection names

## Problem

`inspect_closed` reads persistent XML while a domain runs and reports its active
flag. Its name and `ClosedDomainInspection` imply a stopped-state prerequisite
that the implementation does not enforce or need. The existing running-domain
inspection test demonstrates why persistent and live XML must remain distinct.

## Scope

Rename the existing session-owned result to `PersistentDomainInspection` and
method to `inspect_persistent_definition`. Migrate the exact seven source/test
files in the frozen census, including protocol, materializer annotations, caller
doubles and existing test invocations. Replace the pre-stop diagnostic's
"closed domain inspection" wording with "persistent domain inspection".
Definitions stay in `session.py`; no transition of ownership or compatibility alias.
Keep the result fields, frozen shape, inactive-XML flag, projected validation,
XML/identities, observed `active`, separate `require_inactive`, and session fences.
No cleanup-layout migration, callback changes, new modules, wire/schema changes,
auth policy, dependencies or deployment. Those other findings retain their owners.
This is an internal pre-release naming repair, not a new state precondition.

### Failure model

- Actors/deployments: local-libvirt session callers on supported Linux hosts;
  local proof uses fake libvirt boundaries on real x86_64.
- Assets: persistent configuration identity, observed power state and mutation fences.
- Accepted classes: power may change after inspection; no atomic power snapshot is added.
- Covered elsewhere: libvirt/real guest and hardware qualification retain live tiers.

## Success

The seven-file census has one renamed result class and the renamed protocol and
implementation methods, with every actual direct caller migrated and old aliases
removed. Normalized ASTs preserve behavior outside these names and diagnostic text.
The existing running-domain test proves persistent XML with `active=True` before
stop and identical XML with `active=False` afterward. Existing ownership/projected
XML rejection, closed-session and independent inactivity checks remain effective.

## Validation

`task-test-not-applicable`: the pure internal rename and diagnostic wording have
no new behavioral failure; do not invent a name/prose snapshot or artificial RED.
`focused-test`: strengthen existing
`test_inspection_reads_the_persistent_definition_of_a_running_domain` in
`tests/providers/local_libvirt/lifecycle/boot/test_session.py` with active assertions;
old-source preservation GREEN is expected. Controlled inactive-XML and active-flag
faults must fail that existing test, then exact restoration must pass. Run
`just test-verbose` on the four census test files, plus lint, whole types and hooks.
Compare renamed ASTs and unchanged SQL; independent design/code/security review,
conventional local commit and exact finding attribution precede completion.
