# 0705 — Normalize copied container runtime input permissions

## Status

Accepted (2026-09-28)

## Context

Docker COPY preserves checkout modes. A controlled 0775/0664 source context fails the final
manifest build with `fingerprint_ancestor_replaceable`, even though its bytes are unchanged.

## Decision

After the final source and manifest-script COPYs, apply `chmod -R a+rX,go-w` to those copied
inputs before generating the bootstrap manifest. Keep ownership and fingerprint validation strict.

## Consequences

Runtime files remain readable and directories traversable; executable bits and bytes survive.
Group/world write is removed only in the image. Build permission errors fail the layer.

## Considered & rejected

- Do nothing — [observed] the controlled Docker build exits 1 at manifest generation with
  `fingerprint_ancestor_replaceable` and mode 0775; issue #2864 remains reproducible.
- Relax fingerprint validation — [judgment] contradicts the required immutable input contract.
- Normalize the caller checkout — [judgment] places image correctness on mutable host setup.
