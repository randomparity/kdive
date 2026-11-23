# Local owned-domain identity ownership

## Problem

The operation/session module owns pure XML ownership checks and semantic identity
also used by independent System-authority code. That consumer imports session
lifetime and transport machinery to obtain a deterministic identity function.

## Scope

Move `_parse_owned_xml`, `owned_system_semantic_identity`, `_disk_source_is`,
`_rendered_cpu_projection`, `_canonical_os_type_attrib`, `_memory_bytes` and
`_digest` from `local_libvirt/lifecycle/boot/session.py` to `owned_domain.py`
in the same package. Name the shared parser `parse_owned_xml`; retain the identity
function's name. Reuse existing shared XML parsing/projections and architecture
machine-alias rules with their complete pure dependency closure.
Session operations import the parser directly; System authority and both existing
identity test modules import the new owner. Remove old definitions and forwarding
imports that are no longer needed. Leave guest-agent checks, acceleration constants,
operation pins, locks, cleanup and storage traversal in session.
Preserve ordinary/projected NFC and bounded defused parsing, name/metadata checks,
unique writable virtio-vda qcow2 overlay ownership, memory-unit normalization and
exact semantics-v2 canonical JSON/digest prefix. Add no identity rule or schema.

### Failure model

- Prevent: independent identity consumers depending on session ownership machinery.
- Detect: missing dependency/import migration, changed digests or ownership refusal,
  and altered normalization across x86_64 and ppc64le fixtures.
- Recover: revert the local extraction; persisted identities need no migration.
- Accepted: this unit does not reorganize session traversal or transport mechanisms.

## Success

1. Pure owned-domain validation/identity has one owner independent of session.
2. Direct consumers migrate without compatibility wrappers or a reverse import.
3. Existing identity bytes and validation behavior remain unchanged.

## Validation

- `Mode: focused-test`: add a structural check for the new owner's independence
  from session; demonstrate missing-owner red, green, and a reverse-import fault.
- `Mode: task-test-not-applicable`: relocation changes no algorithm; compare all
  seven definition ASTs after the parser rename and inspect direct imports.
- `Mode: focused-test`: capture a literal digest from the unchanged deterministic
  XML fixture and add unknown-unit, noninteger and negative-memory cases. Existing
  session tests cover machine/CPU normalization, backing-store drift, native readback
  and duplicate overlays. Prove the golden/error cases detect controlled faults.
- Run the existing session and System-authority files with `just test-verbose`,
  `just lint`, whole-tree `just type`, documentation checks and configured hooks.
