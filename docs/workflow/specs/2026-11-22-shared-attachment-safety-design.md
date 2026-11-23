# Shared libvirt attachment safety

## Problem

Local teardown and authority device identity currently import generic attachment
safety from the remote provider. The shared safety traversal and bounded XML
parser belong below both providers, independently of appliance validation.

## Scope

Move host stat identity, foreign-reference proofs, storage/path traversal,
ownership metadata checks, identity validation, required protocols and constants
into `providers/shared/libvirt_attachments.py`. Move `xml_bounds.py` into shared
ownership as `libvirt_xml_bounds.py`. Remote attachment inspection imports shared
primitives; appliance expectations and validation remain remote-owned.

Update direct source/test consumers and dynamic import strings. Shared primitives
used by remote inspection receive descriptive internal names rather than private
cross-module imports. Preserve function bodies except renamed internal calls.
No forwarding modules, dependency changes, public API changes or persisted-data
changes are needed. Existing ADR-0585 and ADR-0603 decisions remain unchanged.

### Failure model

- Deployment: existing local and remote libvirt on Linux x86_64 and ppc64le.
- A reverse import can retain the provider coupling; reject it structurally.
- Dropped symbols and obsolete patch targets can break inspection; migrate all
  direct consumers and verify existing tests and whole-tree types.
- Preserve active/inactive inspection, backing aliases, XML/identity budgets,
  error categories/messages, metadata ownership rules and domain handle cleanup.
- Accepted: this ownership move does not redesign the inspection algorithm.

## Success

1. Shared safety loads without importing either concrete provider.
2. Local teardown and authority identity use shared ownership directly.
3. Existing safety behavior is unchanged and appliance policy remains remote.

## Validation

- `Mode: focused-test`: add a fresh-process shared-import boundary test, observe
  failure before extraction and failure under a controlled reverse import.
- `Mode: focused-test`: run existing remote attachment/XML bounds, local boot
  and authority identity tests, covering aliases, budgets and cleanup errors.
- Compare moved AST bodies with originals after normalizing internal renames;
  inspect every changed import and patch target.
- Run `just lint`, whole-tree `just type` and documentation checks. Commit locally.
