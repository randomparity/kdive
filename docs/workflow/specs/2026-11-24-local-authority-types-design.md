# Known local authority adapter types

## Problem

The private provisioner protocol and composition adapter erase known inputs through Any and
arbitrary kwargs. Three teardown declarations similarly erase an existing four-boolean record.
The concrete provisioner and authority caller already define the exact contracts.

## Scope

Type both private provision signatures with existing ProvisioningProfile and five known
keywords: overlay_customizers, bootstrap_pubkey, selected_gdb_port, selected_ssh_port and
before_extract_baseline (C1). Copy concrete defaults/types using OverlayCustomizer and
BaselineKernel. Forward each value explicitly, plus the adapter's configured expected_guest_arch.
Remove the redundant private profile isinstance and arbitrary kwargs; no new abstraction.

Use existing LocalSystemTeardownInspection for the session protocol, inspection-return helper
and absence-facts argument (C2). Replace structural inspection classes in the existing authority
tests with that record. Preserve prior facts; fill previously unused fields from fixture state.
No type relocation, caller migration or compatibility path is needed beyond these local doubles.
No ADR is needed to restore contracts already declared by concrete owners.

### Failure model

Validated authority snapshots supply profiles; the adapter's private interface admits its five
known keyword inputs. The type gate must reject misspellings and nonexistent inspection fields.
Concrete provisioning, configured architecture, persistence, teardown fencing, cancellation and
cleanup remain with existing owners. No compatibility promise for invalid untyped internal
calls, new runtime validation policy, public wire/schema or permission/concurrency change.
The existing record's fields and semantics remain unchanged.

## Success

Whole-tree type checking can see forwarded keyword types and four inspection facts (C1/C2).
Temporary invalid keyword/field probes pass through baseline Any but fail after repair.
Valid provider operations retain architecture, inputs and absence/teardown behavior (C3).
Commit locally and attribute the exact finding; other session steps remain pending (C4).

## Validation

focused-test: controlled source faults at the actual provision call and inspection consumer,
checked with whole-tree just type, demonstrate baseline acceptance and repaired rejection;
restore exact bytes after each. Run just test-verbose with
 tests/providers/local_libvirt/test_system_authority.py,
 tests/providers/system_authority/test_composition.py,
 tests/providers/local_libvirt/test_provisioning.py and
 tests/providers/local_libvirt/lifecycle/boot/test_session.py.
Run lint, whole-tree types and hooks. Protect198SQL and unrelated executable AST; verify the
explicit forwarding and configured architecture through existing real provisioning evidence.

task-test-not-applicable: no annotation/private-signature snapshots or prose tests; the existing
type checker is the executable structural proof. No new runtime behavior or hardware deployment
is claimed. Scoped reviews and final campaign gates retain their existing owners (C4).
