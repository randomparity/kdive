# 0572 — Build :edge amd64-only; releases stay multi-arch

## Status

Accepted (2026-08-21)

## Context

[ADR-0359](0359-multiarch-app-image.md) publishes `ghcr.io/randomparity/kdive` as a
`linux/amd64,linux/ppc64le` manifest from `release-image.yml` on every push to `main` and on
every `v*` tag. Its *Emulated-build-time tradeoff* accepted ~4 hours of amd64-runner time per
build because "the job runs only on `main` pushes and release tags" — a cadence assumption of
*rarely*. Sizing that job's `timeout-minutes` for #1983 surfaced what the actual cadence costs:
across the last 19 successful runs — all of them main pushes — job duration (queueing excluded)
was median ~235 min and max 248, and with the workflow's
`concurrency: { cancel-in-progress: false }` group consecutive merges queued 60–100 minutes
behind each other. Every merge to `main` pays roughly four hosted-runner hours for a rolling
`:edge`.

The cost is entirely the emulated `linux/ppc64le` leg: under QEMU on the amd64 runner,
`grpcio`, `drgn`, and `libvirt-python` compile from source. An amd64-only build compiles
nothing from source — `ci.yml`'s PR build of the same `Dockerfile` on native amd64 is observed
at ~2.5 minutes. The two `v*` tag builds to date ran 132 min (`v0.3.0`) and 241 min (`v0.4.0`);
a tag build does strictly more work (per-platform SBOM and max provenance) and is where the
emulated leg still belongs.

## Decision

We build **only `linux/amd64` on a push to `main`**, and both `linux/amd64,linux/ppc64le` on a
`v*` tag: `:edge` stays cheap and releases stay multi-arch. This supersedes
[ADR-0359](0359-multiarch-app-image.md): its multi-arch release decision is carried forward
unchanged — a `vX.Y.Z` tag still publishes both platforms from the same job, with the same
Dockerfile hardening and `libkdumpfile` requirements — and what is withdrawn is only its
acceptance of the emulated leg on every commit.

## Consequences

- `:edge` and `:sha-<short>` become amd64-only. A POWER host has no rolling tag to pull between
  releases; it pins the newest `:X.Y.Z`. `docs/development/releasing.md` says so in the same
  change.
- The per-commit runner cost falls from ~235 min to the native amd64 build plus layer push —
  provisionally bounded ≲30 min pending observation, sized from the observed ~2.5-min amd64 CI
  build (which does not push layers). Per the #1983 convention the new figure is observed,
  recorded in the workflow comment, and re-sized once real main-push runtimes exist.
- Queueing behind `release-image-refs/heads/main` shrinks proportionally with the job runtime;
  `cancel-in-progress: false` itself is unchanged here.
- The timeout bound is shared by both shapes of the one job and must cover the tag case; the
  fired-timeout risk window around an already-pushed release image is unchanged, and closing it
  remains #1991's unsigned-digest restructuring.
- A future wall-time concern does not reopen this record's shape choice blindly: per-platform
  jobs feeding a manifest-merge job remain available (below), and #1991 will restructure this
  workflow regardless.

## Considered & rejected

- **Per-platform jobs feeding a manifest-merge job** (the cosign step moves to the merge job,
  because `steps.build.outputs.digest` would live on a platform leg). Rejected for this issue:
  concurrent legs halve wall-clock but keep both legs running per commit, so the standing ~4
  runner-hours per merge the issue names survive untouched. Available later if wall time, not
  runner spend, becomes the constraint.
- **Keep the current shape and record why the cost is accepted.** Rejected: the benefit of a
  multi-arch `:edge` per commit is speculative (no observed ppc64le `:edge` consumer between
  releases), while the cost is measured at ~4 h per merge plus queue delay for everything else
  in the concurrency group.
- **Native ppc64le runner / scheduled-only ppc64le builds.** Already weighed in ADR-0359 and
  rejected there; nothing here changes that reasoning.
