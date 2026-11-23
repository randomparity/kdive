# Remote boot command-line validation reuse

## Problem

Remote preparation duplicates the shared XML character predicate and carries a
stale comment claiming ordinary ExternalBootPlan validation admits illegal C0
controls. The domain plan now rejects these before either provider runs.

## Scope

Rename shared _require_cmdline to require_cmdline and retain its use by
render_target_xml. Remote prepare_target_definition calls it through its
existing _definition import. Delete the remote predicate and stale comment.
Map LibvirtDefinitionError to the existing conflict message and cmdline-xml
rule with the same System identity, chaining the cause. Keep the guard after
ownership, plan identity and initrd checks, before artifact/disk validation.
The shared predicate still owns artifact-path character checking. Document
why carriage return fails exact round-trip despite being legal XML.
This is internal reuse; public/domain/wire contracts and error policy stay.
Python 3.14 managed by uv; existing x86_64/ppc64le targets; local commits only.

### Failure model

- Actors and deployments: local/remote libvirt definition preparation on
  x86_64 and ppc64le; trusted worker supplies a validated external-boot plan.
- Invariants and assets: preserve exact command-line text and reject XML-illegal
  controls, surrogates, U+FFFE/U+FFFF and carriage-return folding at this guard.
- Accepted failure classes: remote guard rejection cannot normally arise from
  a validated plan; preserve its defensive mapping without relaxing the plan.
- Covered elsewhere: existing artifact path, ownership, disk and XML parse gates;
  existing domain plan tests retain their rejection before provider entry.

## Success

1. Shared and remote preparation use one existing command-line character rule.
2. Shared rendering rejects U+0000, U+0001, U+000D, U+D800, U+FFFE and U+FFFF
   with the existing LibvirtDefinitionError message and retryable=False.
3. Shared rendering preserves tabs, newlines and U+1F600; remote preparation
   preserves accepted decomposed text and U+1F600. Guard ordering stays intact.

## Validation

- Mode: focused-test. Parameterize the existing shared rejection and accepted
  scalar tests, and the remote decomposed-plan test; keep exact parsed XML text.
  These are behavior-preserving cases expected to pass before implementation.
- Mode: focused-test. A model copy deliberately bypasses plan validation with
  an XML control; the real provider guard must reject before an invalid artifact
  path, retain conflict/System/rule and chain LibvirtDefinitionError. Expected
  red: the old duplicate guard lacks that cause. Existing remote plan rejection,
  artifact, ownership and preparation tests retain current conflict/order behavior. Run just test-verbose
  tests/providers/shared/test_libvirt_external_boot.py and
  tests/providers/remote_libvirt/lifecycle/test_external_boot.py together.
- Mode: focused-test. Mutate the shared predicate to admit carriage return and
  reject U+1F600 in turn; new regression cases must fail, then restore. Run
  just lint, whole-tree just type, doc checks and commit hooks after restoration.
