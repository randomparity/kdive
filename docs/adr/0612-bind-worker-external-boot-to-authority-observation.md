# 0612 — Bind worker external boot to authority observation

## Status

Accepted (2026-09-06)

## Context

The provider authority owns privileged local guest and storage access. A worker can construct
an authenticated sender but cannot construct the authority's operation lease or open its private
provider socket. The existing worker handler still reads the running kernel through a direct
provider port after receiving a journaled mutation result. That leaves the production handler
unconstructible without restoring the privileged worker access the authority boundary removes.

## Decision

Resolve the authority route from the job's fixed Resource binding in worker assembly. One
operation-bound client supplies takeover, preparation, mutation and observation calls using the
active incarnation credential borrowed during encoding. The client validates the requested System,
provider and authority instance against that binding and uses one absolute monotonic deadline for
its invocation. Endpoint selection and credential references remain configuration-owned. Missing
or mismatched bindings fail before authority allocation or provider mutation.

Running-kernel observation uses a separate closed, read-only authority operation. It shares the
ordinary observation gate: authenticate the peer, verify the current binding and anchored journal
head, retain the System lane through the actual provider read, then recheck authority and head.
The local adapter obtains its operation-scoped lease inside the completion-owned provider thread.
The worker compares the returned kernel identity and exact command-line bytes before core success.

The response is not a journal record. It contains the existing kernel identity and canonical
lowercase hexadecimal command-line bytes, each capped at 2,048 decoded bytes. Hexadecimal encoding
preserves invalid UTF-8 without an implicit text conversion. Existing version-1 journal fields,
canonical bytes and hashes do not change. Neither the read operation nor its response authorizes
provider mutation or chooses a destination, path or command.

## Consequences

Workers no longer need a privileged direct provider read to complete an authority operation.
Incomplete optional authority configuration cannot interfere with unrelated lifecycle operations;
authority calls still fail closed. Configuration alone is not installed readiness or native proof.
The local and remote installed operation chains remain subject to their deployment and acceptance
tests before being advertised as usable.

## Considered & rejected

- **Install authority provider credentials in the worker.** This breaks the role boundary.
- **Synthesize a running-kernel observation from the plan.** Intended state is not observed state.
- **Add kernel bytes to the retained journal observation.** This changes canonical version-1
  records and persists guest output unnecessarily.
- **Start a nested event loop behind a synchronous provider facade.** Worker handlers already
  support awaited authority operations; another loop is unnecessary.
