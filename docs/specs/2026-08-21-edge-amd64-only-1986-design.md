# #1986 — `:edge` builds amd64-only; releases stay multi-arch (design)

Date: 2026-08-21 · Issue: #1986 · Decision record: [ADR-0572](../adr/0572-edge-builds-amd64-only-releases-stay-multiarch.md) (supersedes [ADR-0359](../adr/0359-multiarch-app-image.md))

## Problem

`release-image.yml` spends ~4 hosted-runner hours on every push to `main` (median ~235 min,
max 248 over 19 runs, queueing excluded) because the emulated `linux/ppc64le` leg compiles
`grpcio`, `drgn`, and `libvirt-python` from source under QEMU. With
`cancel-in-progress: false`, consecutive merges queue 60–100 min behind each other. ADR-0359
accepted that cost under a "runs rarely" cadence assumption the data now falsifies.

## Options

1. **amd64-only on `main`, multi-arch on `v*` tags** — cuts the per-commit cost to the native
   amd64 build (observed ~2.5 min in `ci.yml`'s PR job, plus layer push); releases keep both
   platforms. Cost: `:edge` is single-platform between releases.
2. **Per-platform jobs feeding a manifest-merge job** — halves wall clock, keeps ~4 runner-hours
   per commit. The cosign step must move to the merge job because
   `steps.build.outputs.digest` lives on a platform leg.
3. **Keep the shape, record the acceptance** — zero risk, keeps the measured cost.

## Decision (ADR-0572)

Option 1. The issue names standing runner spend as the cost; only option 1 reduces it. Option 2
addresses wall time, not spend; option 3 pays a measured cost for a speculative benefit (no
observed ppc64le `:edge` consumer between releases).

## Change surface

- `.github/workflows/release-image.yml`: `platforms:` becomes
  `${{ startsWith(github.ref, 'refs/tags/v') && 'linux/amd64,linux/ppc64le' || 'linux/amd64' }}`;
  the QEMU step becomes tag-only; header and step comments state the two shapes; the cosign step
  is untouched (it signs whatever digest the build pushed).
- `timeout-minutes` re-sized 350 → 300 with the #1983 convention: the binding case is the tag
  build (observed 132/241 min, n=2, provisional); the main-push shape is provisionally ≲30 min
  pending observation.
- `docs/adr/0572-edge-builds-amd64-only-releases-stay-multiarch.md` new;
  `docs/adr/0359-multiarch-app-image.md` status region gains the supersession banner and
  status link (both spellings gate-checked); `docs/development/releasing.md` multi-arch
  paragraph updated.

## Exclusions

#1991 (unsigned-digest window restructuring of this workflow, after this PR merges); #1183
(ppc64le runtime proof); `cancel-in-progress` semantics.

## Verification

`just lint-workflows` (zizmor + actionlint), `just adr-status-check`, the records gate
(`.github/scripts/check-records.sh`, adr profile), and the workflow-parsing guard tests.
