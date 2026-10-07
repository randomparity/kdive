# Lifecycle installer root uv selection

## Problem and authority

Issue #3069 and frozen scope comment6047709804 require the host play to use its
installed root uv even when sudo PATH omits `/usr/local/bin`, reject unsuitable
selection before avoidable installer mutation, and preserve default callers.
Approved exclusions: sudo policy weakening; tool isolation #3070. Native POWER
qualification remains separate. Full-spec S denominator100 is fixed from3adae3991.

The existing `_resolve_uv_bin` ignores provisioning's `live_vm_host_uv_bin` and
only calls `command -v uv`. A source-only reproduction with restricted PATH and
an existing explicit executable fails before implementation. No host reset is
part of this change.

## Design

[ADR-0727](../../adr/0727-explicit-lifecycle-uv-selection.md) records the optional
`--uv PATH` contract. The lifecycle host-play argv carries the existing root uv
variable; its provisioning/default comments stop claiming sudo includes that path.
The installer parses the option and passes it to its existing resolver before
reading the witness DSN or mutating the host. No new installation task is added.

For explicit selection, require an absolute executable regular file; canonicalize
with GNU `readlink -e`, then inspect its file and ancestors with GNU `stat`.
Reject a non-root owner or group/other-write bit. Execute only the validated
canonical path. A supplied empty value is invalid rather than requesting fallback.
Resolve default callers with the existing `command -v` behavior and absolute-file
check, without extending their PATH. A successful `--version` output beginning
with `uv ` and a numeric version rejects an unrelated or nonworking selection.
Do not execute an explicit candidate before ownership/mode checks pass.

Errors identify selection and remedy and exit nonzero before installer mutation;
no DSN is consumed or logged by selection. Remove the current error's advice to
inherit operator PATH. Keep existing default caller documentation; document the
provisioning option and validation requirements beside installer usage.

## Failure model

- Actors/deployments: root installer on supported Linux hosts, Ansible inventory
  selecting installed root uv, and existing direct PATH-based callers.
- Invariants/assets: provisioning uses its one selected executable; unprivileged
  writers cannot replace the explicit canonical path; invalid selection fails
  before installer mutation and before secret input consumption.
- Accepted classes: trusted root administration may replace files/configuration;
  executable-content authenticity and the existing direct caller's PATH policy
  remain the operator's existing responsibility. Validation is not a race-proof
  identity pin against root. Missing uv fails instead of installing a second copy.
- Other owners: #3070 tool isolation, existing uv package installation/version
  policy, and native POWER qualification. No sudo configuration changes.

Boundary controls: inventory path to root execution is checked for absolute form,
regular executable file, canonical root-owned non-writable ancestry and uv identity.
Paths remain quoted argv, not evaluated shell text. Root filesystem tools are
existing prerequisites. Diagnostic output never includes the witness DSN.

## Success and validation

Tests exercise actual resolver/CLI boundaries: valid explicit executable outside
restricted PATH, absent/relative/empty/directory/non-executable selections,
non-root/writable file or ancestor, invalid version response, and default caller.
Mock ownership metadata at the OS boundary for unprivileged unit tests; retain
real path canonicalization and process execution. Pin host-play argv and secret
censoring and resolver-before-mutation ordering in existing provisioning tests.

Run focused tests red/green, provisioning regression suite, lint, whole-tree type,
shell and owning Ansible checks. Coordinate the authorized Rocky host-play proof
with root after #3068; report actual lifecycle result and any separate prerequisite
failure. Final mandatory pre-push CI and remote CI remain required before handoff.
