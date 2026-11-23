# Process-global secret scope identity

## Problem

`SecretRegistry` maps `scope=None` to the ordinary string `__global__`.
Releasing a caller scope with that string removes process-global registrations too.
The presence projection also mislabels that string as process-global.
This contradicts the existing lifetime and projection contracts and ADR-0027.

## Design and scope

Replace the existing private class marker with one `object()` identity sentinel.
Use only identity in `_scope_label` to recognize it; ordinary strings retain their
verbatim presence label. Keep register/release locking, per-value reference counts,
version increments, empty input behavior, snapshots, explicit clear and signatures.
Introduce no wrapper, enum, new scope type or dependency. No data migration is needed.
The marker remains internal and is not a supported caller scope reference.

## Success

1. Releasing the normal string scope does not evict distinct or shared values also
   registered under None, and version/reference-count behavior remains consistent.
2. Process-global and string scopes produce their documented distinct presence labels.
3. A cached logging filter refreshes after release, still masks the global value,
   and stops masking the distinct released-only value.

## Failure model

- Actors and deployments: KDIVE application/worker code uses app-owned registries;
  supported callers provide None, ordinary string references or normal owner objects.
- Invariants and assets: process-lifetime values remain available for output redaction
  across bounded-scope release; presence output contains references rather than values.
- Accepted failures: explicit clear and process termination discard registrations by
  existing contract; malicious in-process code accessing private state is trusted.
- Covered elsewhere: backend resolution/root confinement and pre-registration output
  quarantine remain their existing owners under ADR-0012/0027.

## Threat model

- Boundary inventory: existing caller scope keys enter the retention mapping; registry
  snapshots feed the existing logging/response redactors. No new or widened boundary.
- Actors: normal bounded-scope input may equal an internal-looking string; it must not
  obtain authority over None-scoped values. In-process private-state mutation is trusted.
- Controls: an unforgeable ordinary-value identity marker separates the global bucket;
  identity labeling preserves the existing value-free presence projection.
- Out of scope: new backend/secret-resolution behavior, arbitrary malicious Python
  objects or private-state mutation; this changes no code execution trust boundary.

## Validation

Add failing registry cases for distinct/shared values under None and `__global__`,
normal scope labels, and version increments; add a cached logging-filter test after
that string is released: mask the global value and expose the distinct released-only
synthetic value in the same record. Run registry/redaction/backend tests and meaningful logging
consumers, then whole-tree lint/type and hooks. Restore the string marker and equality
label faults separately to verify the new tests fail; restore source afterward.
No external secret store, database or live deployment is required for this in-process
contract. Record the spec; keep the implementation plan transient and commit locally.
