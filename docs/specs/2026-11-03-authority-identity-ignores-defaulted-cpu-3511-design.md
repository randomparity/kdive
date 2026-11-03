# Design — the authority System identity ignores a libvirt-defaulted `<cpu>` (#3511)

## Problem

`owned_system_semantic_identity` hashes the `<cpu>` mode and model. KDIVE renders no `<cpu>` for an
unpinned TCG domain, but libvirt stores `<cpu mode='custom' match='exact' check='none'>` for a
ppc64le TCG domain. The stored identity then differs from the intent digest, so the authority does
not recognise its own System (it fails closed).

## Decision

- Project `<cpu>` only in the forms KDIVE renders: `host-passthrough` / `host-model` (KVM), or a
  pinned `custom` element with `check='partial'` (ADR-0369). A `custom` element without
  `check='partial'` is libvirt's default and projects as absent.
- Every KDIVE-renderable form projects exactly as before, so stored digests are unchanged and the
  schema string `kdive-local-authority-domain-semantics-v2` does not move.
- Rejected: comparing against the intent's rendered XML. The identity function is deliberately
  context-free (it digests one document), and the rule above needs no second input.
- Out of scope (operator-approved): the native POWER9 KVM-HV proof (own issue); vmlinux staging (#3130).

## Failure model

A libvirt-added `custom` element that carries `check='partial'` would be hashed and mismatch:
the failure stays fail-closed (the System is not recognised as owned), never a false match.
A tampered stored `<cpu>` of the defaulted shape is ignored; `<cpu>` is not an ownership
boundary (name, uuid, metadata, overlay and kernel paths still are).

## Verification

`tests/providers/local_libvirt/lifecycle/boot/test_session.py`: a defaulted `custom` cpu leaves the
ppc64le and x86_64 identities unchanged; passthrough, pinned, and a different pinned model each move
it. Reverting the projection turns the defaulted-cpu tests red. Live: an authority-owned ppc64le TCG
provision reaches `ready` with no intent-mismatch line, then release and teardown complete.
