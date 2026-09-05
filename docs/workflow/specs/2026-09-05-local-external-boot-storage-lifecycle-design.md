# Local external-boot storage lifecycle design

## Scope

Issue #2245 supplies request-bound reclamation and deployment capacity for local external boot.
It implements [ADR-0602](../../adr/0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md)
under ADR-0586 and ADR-0591. It changes no database schema and needs no migration.

The supported target architectures remain x86_64 and ppc64le. This campaign runs native proof on
x86_64; native ppc64le live proof is explicitly excluded. Production operation-chain binding is
#2246, readiness and legacy-domain migration are #2243, and restart-time tombstone reconstruction
is #2244.

## Required behavior

`CleanupPayloads` changes from `(run_fd, binding)` to `(run_fd, metadata)`. The operation passes
the exact `LocalRecoveryMetadataV1` it has reopened before cleanup. `_ConcreteSession` no longer
calls `require_inactive` for this host-only callback. All guest contexts, XML/overlay mutation, and
guest-tree access retain their existing inactive checks.

`LocalPayloadCleanup` performs these bounded steps:

1. Validate all metadata-derived names before deletion. Parse the target projection reference
   through the existing owner-bound artifact-reference parser and obtain its exact digest.
2. Open the configured root, System, Run, projection, recovery, and partial directories only by
   descriptor-relative no-follow helpers. Require mode 0700 and the current effective owner.
3. Validate the projection sidecar against the metadata ownership and digest before removing the
   fixed payload names and `target-projection.json`, then remove the digest directory.
4. Remove `modules.tar` from the exact complete recovery directory. A matching partial may contain
   only the bounded preparation/pre-stop files and archive temporary/final names. Reopen and parse
   its durable record; delete it only when it authenticates the same binding. Any other contents
   or shape produce a path-redacted `ValueError` before that directory is changed.
5. Prune the Run and System directories with `rmdir`. `ENOENT` and `ENOTEMPTY` mean converged or a
   sibling remains; other errors are reported without a host path. Fsync each changed parent.

Each unlink treats absence as success, so interruption and retry converge. Every candidate name is
fixed or derived from canonical identifiers and authenticated metadata. There is no directory
enumeration that expands the deletion set and no recursive deletion. The complete recovery
directory remains until tombstone finalization; #2244 owns reconstruction after restart.

## Partial residue

A partial is reclaimable only during terminal cleanup for the same activation and only when one
canonical durable record inside it (`pre-stop-intent.json` or preparation receipts) parses and
matches the authenticated binding. A partial with both competing ownership records, unexpected
entries, noncanonical bytes, or a conflicting binding is ambiguous. Symlinked, non-directory,
wrong-owner, or non-0700 entries are likewise refused. Refusal leaves the partial untouched and
uses a fixed diagnostic with symbolic errno only.

This change does not add a background sweep. Request-bound retry is the only deletion authority;
foreign residues are reported for operator handling.

## Provisioning contract

The worker gate allowlist includes `KDIVE_LIBVIRT_RECOVERY_ROOT`, and Ansible writes the exact
per-slot value into each slot's worker environment. Tests execute the gate and prove the child
receives that value without inheriting unrelated ambient variables.

Defaults expose:

- maximum simultaneous activations per worker, unit `activations`, scope one worker;
- minimum free capacity, unit bytes, measured from filesystem free bytes after provisioning;
- the derived worst-case bytes per activation.

The formula is `(kernel + initrd + modules + recovery archive + projection + in-flight recovery
archive partial) * admitted concurrent activations`, using source-code bounds. Provisioning checks
the configured minimum is at least that derived floor, reads the filesystem's available bytes with
an argv-safe command, and fails before worker release when observed space is below the configured
minimum. The failure names the byte requirement and recovery action but not private host identity.
The live-testing runbook carries unit, reference state, scope, consequence, and recovery action.

## Threat model

The authenticated activation metadata crosses from durable provider state into a privileged
filesystem deletion operation. Existing canonical UUID, opaque-reference, canonical-JSON,
owner/mode, and descriptor-relative no-follow checks control it. Unexpected filesystem entries are
controlled by an exact allowlist and fail-closed retention. Errors expose fixed labels and symbolic
errno, never the configured root.

Operator-supplied Ansible variables cross into worker environment and capacity checks. YAML integer
validation bounds positive concurrency/capacity values; `ansible.builtin.command` uses `argv`, not
a shell. The worker receives only its provisioned slot root through the existing environment
allowlist.

Trusted actors are the host operator, the fixed worker account, and the authority process.
Authenticated tenants cannot select filesystem paths. A malicious root operator and recovery of
foreign residue are out of scope: root already controls the service and ambiguous residue is
intentionally retained.

## Verification

Focused tests cover inactive and restored-running cleanup; exact projection and parent pruning;
every unlink/rmdir interruption followed by retry; sibling preservation; partial record/shape,
symlink, mode, owner, and ambiguity refusals; and retained guest mutation gates. Deployment tests
cover the worker environment and all capacity-gate branches. An Ansible check-mode/syntax run and
the authorized x86_64 local-libvirt provisioning path provide clean-host evidence. Repository
guardrails remain the final gate.
