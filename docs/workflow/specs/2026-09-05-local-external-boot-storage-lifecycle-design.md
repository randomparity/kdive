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

### Activation-exclusive artifact layout

The dormant `local-artifact-v1/<system>/<run>/<digest>/<file>` reference is replaced, not migrated,
by `local-artifact-v2/<system>/<run>/<activation>/<digest>/<file>`. `LocalArtifactRoot.open` creates
and returns `<system>/<run>/<activation>`, so fixed payload names from different activations never
share a directory. `TargetProjectionStore` publishes below the same activation component. Parsing
requires all three owner identities. There is no compatibility reader because #2246 has not bound
the production writer; tests prove v1 is refused and two same-Run/same-digest activations remain
independent when one is cleaned.

### Completed activation cleanup

`CleanupPayloads` changes from `(run_fd, binding)` to `(run_fd, metadata)`. The operation passes
the exact `LocalRecoveryMetadataV1` it has reopened before cleanup. `_ConcreteSession` no longer
calls `require_inactive` for this host-only callback. All guest contexts, XML/overlay mutation, and
guest-tree access retain their existing inactive checks.

`LocalPayloadCleanup` performs these bounded steps:

1. Validate all metadata-derived names before deletion. Parse the target projection reference
   through the existing owner-bound artifact-reference parser and obtain its exact digest.
2. Open the configured root, System, Run, projection, recovery, and partial directories only by
   descriptor-relative no-follow helpers. Require mode 0700 and the current effective owner.
3. Validate the activation-exclusive projection sidecar against the metadata ownership and digest before removing the
   fixed payload names and `target-projection.json`, then remove the digest directory.
4. Remove `modules.tar` from the exact complete recovery directory. A matching partial may contain
   only the bounded preparation/pre-stop files and archive temporary/final names. Reopen and parse
   its durable record; delete it only when it authenticates the same binding. Any other contents
   or shape produce a path-redacted `ValueError` before that directory is changed.
5. Prune the activation, Run, and System directories with `rmdir`, in that order. `ENOENT` and `ENOTEMPTY` mean converged or a
   sibling remains; other errors are reported without a host path. Fsync each changed parent.

Each unlink treats absence as success, so interruption and retry converge. Every candidate name is
fixed or derived from canonical identifiers and authenticated metadata. There is no directory
enumeration that expands the deletion set and no recursive deletion. The complete recovery
directory remains until tombstone finalization; #2244 owns reconstruction after restart.

## Partial residue

A normal `prepare` retry reopens the partial and resumes it only when its canonical preparation
receipt or pre-stop intent matches the request's binding, plan, materialization, and authority.

Teardown gets a separate provider-local abort-preparation arm reachable before recovery-point
resolution. The authority adapter passes the authenticated request identities to
`LocalLibvirtExternalBoot.abort_preparation`; the coordinator opens the usual authority-bound
operation and returns `False` when no partial exists. For a receipt-only partial it validates the
receipt and removes its fixed files. For a pre-stop intent it validates the intent, inspects the
domain, and accepts only the recorded source definition with no module or target mutation. If prior
power was running and the domain is inactive, it restarts the source and uses the same bounded
readiness mechanism required by #2243 before deleting evidence. It then removes only preparation,
intent, archive, and archive-temporary names and the exact partial directory. Every step is
idempotent; teardown retries this arm until it returns absent.

Before unlinking the final in-partial ownership record, abort publishes a canonical 0600
root-level receipt bound to the activation, plan, and authority, then fsyncs the root. That receipt
is the sole authority for resuming deletion of an otherwise empty partial after restart. Abort also
removes the receipt-authenticated activation payloads and projection before reporting `removed`;
it removes the abort receipt only after the partial directory is gone and the root is fsynced. A
foreign or malformed receipt fails closed, and exact absence includes the receipt.

`abort_preparation` returns a closed result: `removed`, `absent`, or `not-partial`. `removed` and
`absent` create an adapter-local pending-absence handoff only after the adapter has verified that
`context.phase` is `mutation-started`, its commit point is TEARDOWN, and the admitted request owns
the partial or exact absence just proved. The handoff stores the complete validated
`AuthorityMutationRequestV1`, keyed by operation identity, and can be consumed only by an exactly
equal request. The adapter then constructs a stable `absent` `AuthorityObservationV1` without
calling `_resolve_point`, `_require_matching_identities`, or the RecoveryPoint-dependent state
categorizer. The map is bounded to the existing admitted-lane capacity and evicts its oldest entry;
an equal same-process request consumes the handoff. A mismatched request cannot consume or replace
an entry.

Commit and recovery observation both re-run the complete non-mutating exact-absence inventory
before returning a pending terminal handoff. In-process caching therefore cannot hide activation
residue or differ from the result after an adapter restart.

`AuthorityMutationAdapter` adds
`observe_recovery(request, AuthorityRecoveryObservationContextV1)`. The closed context carries only
the exact journal phase (`mutation-started` or `provider-returned`), TEARDOWN commit point,
operation identity, attempt id, journal sequence, and journal digest already verified by the
service. `_finish_recovery` uses this call for those two phases; every other observation continues
through read-only `observe(request)`. The service rejects a context/request or record mismatch
before adapter dispatch, and adapters reject unsupported recovery contexts.

The local recovery call never deletes. If the equal handoff is absent after eviction or adapter
restart, it first classifies matching complete metadata and cleanup tombstones through their
existing authenticated paths. Only when both are absent does a descriptor-relative
owner/mode/no-follow probe verify that the canonical partial and activation-owned cleanup artifacts
are also absent. Missing partial evidence alone is not terminal. Present owned residue is
nonterminal; malformed state, I/O failure, or any context/request mismatch returns
`provider_conflict`; only the ordinary authenticated commit arm may remove present residue. This
separates non-mutating restart proof from context-authorized
deletion. A present malformed/foreign partial raises `provider_conflict`; an I/O failure is
bounded to `provider_conflict` and is never converted to absence. `not-partial` falls through to
normal complete-recovery teardown. Lost response and already-absent retries return the same
observation id and category.

A partial with competing ownership records, unexpected entries, noncanonical bytes, or conflicting
identity is ambiguous. Symlinked, non-directory, wrong-owner, or non-0700 entries are likewise
refused. Refusal leaves the partial untouched and uses a fixed diagnostic with symbolic errno only.

This change does not add a background sweep. Prepare retry and authenticated teardown are the only deletion authorities;
foreign residues are reported for operator handling.

## Provisioning contract

The worker gate allowlist includes `KDIVE_LIBVIRT_RECOVERY_ROOT` and
`KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES`. Ansible writes each exact per-slot value into the
slot's worker environment. Tests execute the gate and prove the child receives them without
inheriting unrelated ambient variables.

Defaults expose:

- maximum simultaneous activations per worker, unit `activations`, scope one worker;
- per-activation capacity ceiling, unit bytes, shared unchanged with runtime;
- minimum free capacity, unit bytes, measured from filesystem free bytes after provisioning.

Before its first write, the local materializer computes
`kernel declared bytes + initrd declared bytes + module archive bound from declared uncompressed
bytes/member count + fixed recovery archive maximum + fixed projection/metadata overhead + one
fixed in-flight recovery-archive maximum`. Existing closed plan fields bound every variable term;
the local metadata models gain corresponding upper bounds for materialized bytes. If the result is
greater than the configured per-activation ceiling, materialization fails without writing.

Provisioning computes `capacity ceiling * admitted concurrent activations`. It validates both
inputs as positive integers, reads the filesystem's available bytes with
an argv-safe command, and fails before worker release when observed space is below the configured
derived minimum. The failure names the byte requirement and recovery action but not private host identity.
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

Focused tests cover inactive and restored-running cleanup; exact projection, activation, and parent pruning;
every unlink/rmdir interruption followed by retry; sibling preservation; partial record/shape,
symlink, mode, owner, and ambiguity refusals; and retained guest mutation gates. Deployment tests
cover the worker environment and all capacity-gate branches. An Ansible check-mode/syntax run and
the authorized x86_64 local-libvirt provisioning path provide clean-host evidence. Repository
guardrails remain the final gate.

The partial proof drives the real adapter teardown path for three crash windows: preparation receipt
only, pre-stop intent before stop, and published archive before completion rename. It also proves a
same-Run/same-digest sibling remains openable after the first activation's terminal cleanup.
First execution, lost-response retry, already-absent retry, handoff mismatch, bounded eviction,
adapter restart through the real service recovery call, malformed partial, and read failure each
assert the exact terminal/nonterminal authority observation contract.

Capacity tests exercise equality and one-byte-over cases against the real local materializer before
any artifact file exists, then prove the Ansible-derived minimum uses the same environment value the
worker parses. A changed ceiling therefore affects admission and provisioning together.
