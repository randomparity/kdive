# ADR 0370 — Container image version provenance via build-arg buildinfo

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** kdive maintainers

## Context

ADR-0041 decision 5 defines a self-describing version string: `kdive.version.full_version()`
renders `X.Y.Z+g<sha>` for a clean exact-tag release and `X.Y.Z-dev+g<sha>` otherwise, resolving
the commit + release flag from a baked `kdive._buildinfo` module, then live git, then unknown.
`--version` and the startup log both use it.

The wheel/sdist path bakes `_buildinfo.py` at build time (`release.yml` → `just build true` →
`scripts/stamp-buildinfo.sh true`). The **published container image** does not: `.dockerignore`
excludes `.git`, the final image stage copies only `/opt/venv` + `/app/src`, and the Docker build
never runs the stamp. Inside a released `ghcr.io/randomparity/kdive:X.Y.Z` container,
`python -m kdive --version` therefore reports `X.Y.Z-dev` — no commit, no release flag — because
both the baked-module and live-git resolutions return `None`. This contradicts the `releasing.md`
contract that the SHA/flag "come from a baked `_buildinfo.py` in artifacts," for the one artifact
operators actually run. The multi-arch image is the headline deliverable of the v0.3.0 cut
(#1211, epic #1199 Workstream E), so its provenance must be honest.

A hermetic container build has no `.git` by design (small context, stable cache), so the commit
and release flag must be conveyed in from the workflow, which knows the built ref.

## Decision

We will bake `_buildinfo.py` into the container image during the Docker build from **build
arguments**, reusing `scripts/stamp-buildinfo.sh` as the single source of the file's format:

- `stamp-buildinfo.sh` accepts an explicit commit via the `KDIVE_BUILDINFO_COMMIT` environment
  variable, falling back to `git rev-parse` when unset (today's behavior, unchanged for the
  wheel path and every existing caller).
- The `Dockerfile` builder stage declares `ARG KDIVE_COMMIT` / `ARG KDIVE_RELEASE=false` (placed
  after the dependency-sync layers so a changing commit never busts the `uv sync` cache) and runs
  the stamp only when `KDIVE_COMMIT` is non-empty. The generated `/app/src/kdive/_buildinfo.py`
  rides into the final image on the existing `COPY --from=builder /app/src /app/src`.
- `.dockerignore` excludes `src/kdive/_buildinfo.py` so a developer's leftover local stamp can
  never be copied in and baked stale.
- `release-image.yml` passes `KDIVE_COMMIT=<short sha>` and
  `KDIVE_RELEASE=<is a vX.Y.Z tag>` for both its triggers (main → `-dev+g<sha>`,
  tag → `+g<sha>`).

## Consequences

- A published image reports the same self-describing version as the wheel for the same tag, and
  the same short `<sha>` (both from `git rev-parse --short HEAD` at that ref). Provenance is
  honest for the artifact operators run.
- One `_buildinfo.py` format, one stamp script, for both artifact paths.
- A local `docker build` (or the ci.yml push-less PR build) with no build-arg is unchanged — the
  stamp is skipped and the image reports `X.Y.Z-dev`, exactly as today, rather than a misleading
  `unknown` commit.
- New obligation: the Dockerfile ↔ workflow wiring is a silent-failure surface (dropping the
  build-args would quietly restore the bug), so a structural guard test asserts both ends stay
  wired. The unit gate cannot build an image, so this guard is load-bearing.

## Alternatives considered

- **Un-ignore `.git`, rely on live-git resolution in the container.** Ships history in every
  layer, bloats the image, busts cache on every commit, and would still need `.git` in the slim
  final stage. Rejected.
- **Duplicate the `_buildinfo.py` heredoc in the Dockerfile.** Two sources of the format drift.
  Rejected in favor of reusing the stamp script.
- **Carry provenance only via OCI labels.** Already present; does not fix the runtime `--version`
  / startup-log report, which is the contract. Rejected.
- **`pip install` a baked wheel into the image.** A larger refactor of the `uv`-from-source image
  build for no provenance benefit over the build-arg stamp. Rejected.
