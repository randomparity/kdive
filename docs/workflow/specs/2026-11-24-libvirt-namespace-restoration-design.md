# Restore libvirt XML prefixes at serialization

## Problem

The shared namespace helpers cache registration booleans even though ElementTree's registry
can change afterward. A second render can emit another prefix for KDIVE metadata or QEMU
arguments. Existing local, remote and install renderers require deterministic prefixes.

## Scope

Remove the two cached flags from `providers/shared/libvirt_xml.py`; keep both existing
registration functions and always register their prefix (C1). Move their existing calls directly
before serialization in local normal/customization XML, remote domain XML and local install's
`_render_direct_kernel_xml` (C2). Registration stays lazy. No ownership transition or alias is
needed; construction and serialization remain in their existing modules.

Keeping flags plus registry inspection couples the helpers to private ElementTree internals.
A new serializer or lock adds surface without covering unrelated registry writers. Calling the
existing stdlib registration function at each boundary is the smallest coherent repair.
The existing prefix contract in ADR-0218 remains unchanged; no new ADR is warranted.

### Failure model

Restore prefixes after sequential registry replacement; do not promise safety against concurrent
unrelated mutation. Preserve defused parsing, metadata identity, guest arguments, security and
network policy. Test registry changes must be fixture-owned and restored. No VM/operator or
external mutations, schema, dependency, public signature or persistence changes (C3/C4).

## Success

Each of the four renderers emits literal `qemu:commandline` after registry replacement;
metadata-bearing renderers also restore `kdive` prefixes. Metadata and gdbstub/SSH/egress
arguments remain intact. Imports do not register namespaces; obsolete flags disappear.
Commit locally and resolve only the exact namespace finding after proof (C5).

## Validation

focused-test: extend the existing shared XML, local provisioning/customization, remote
provisioning and local install test modules for repeated registry replacement and actual XML
output (C3). Prove new regressions RED against the cached helpers, then GREEN after repair.
Keep import-no-registration coverage; replace registration-count tests and deleted-flag patches.
Run the five queued test files, lint, whole-tree types and commit hooks. Compare protected
AST/signatures and existing migration hashes; review the final diff and obtain scoped code and
security review before the conventional commit (C4/C5).

task-test-not-applicable: no test of design prose or helper invocation counts; correctness is
observable in serialized XML. No live VM proof is needed for this serialization-only repair.
Final whole-campaign CI and branch reviews remain owned by the enclosing campaign.
