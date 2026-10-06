# 0693 Pinned external kernel fixtures

## Status

Accepted (2026-09-27)

## Context

Issue #2806 needs two real source-built kernels whose boot, debug, module and config outputs
can be reused with their provenance. Compilation stays outside KDIVE (ADR-0316); the existing
live-stack spine owns combined bundle creation and upload. The operator approved this design.

## Decision

Keep source verification in the existing fetch helper. Build into fresh Kbuild output directories
using explicit source commits, target architecture and config inputs. A versioned manifest beside
the outputs records their digests, source/config/toolchain provenance and build ID; its canonical
digest identifies the fixture. Verify the manifest and bytes before qualification reuse.

Use the existing bundle/upload spine to qualify both selected x86_64 baselines with unbound Runs.
Retain exact upload digests and candidate-bound results. Compilation, fixture records and live
evidence remain developer/test tooling, with no new production API or service.

## Consequences

Reusing a mismatched or dirty source tree now fails instead of silently accepting it. Existing
in-tree build outputs ignored by Git remain permitted for existing callers. Partial fixture
builds are retained for diagnosis but cannot be reused; retry in a fresh output directory.
Toolchain/package changes may produce a different fixture identity requiring requalification.
Native POWER and boot/debug qualification remain later epic entries.

## Considered & rejected

- Build inside each live test. judgment: repeats expensive work and obscures reusable fixture identity.
- Reuse a source tree merely because it exists. verified: scripts/fetch-kernel-tree.sh at
  fbaeb9c489 checks only for a .git directory, without checking HEAD or source modifications.
- New build service or fixture scheduler. judgment: outside the approved scope and unnecessary
  for sequential operator-run builds.
