# Container image version provenance (#1211)

- **Status:** Draft
- **Date:** 2026-07-16
- **Issue:** [#1211](https://github.com/randomparity/kdive/issues/1211) — v0.3.0 cut (epic #1199, Workstream E)
- **ADR:** [ADR-0370](../adr/0370-container-image-version-provenance.md)

## Problem

The version-reporting scheme (ADR-0041 decision 5) renders a self-describing version string
from `kdive.version.full_version()`:

- `X.Y.Z+g<sha>` for a clean, exact-tag **release** build, and
- `X.Y.Z-dev+g<sha>` for anything else (a dev checkout, an `:edge`/main build).

Facts resolve first-hit-wins from a baked `kdive._buildinfo` module, then live git, then
unknown. `--version` and the startup log line both call `full_version()`.

The scheme works for two of the three artifact classes:

- **Wheel / sdist** — `release.yml` runs `just build true`, whose `build` recipe runs
  `scripts/stamp-buildinfo.sh true`, baking `_buildinfo.py` (COMMIT + `RELEASE = True`) into the
  built artifact. A `pip install kdive` then reports `0.3.0+g<sha>`.
- **Dev checkout** — no baked module, so `full_version()` falls back to live git and reports
  `0.3.0-dev+g<sha>`.

It is **broken for the published container image** — the headline multi-arch deliverable of
#1211:

- `.dockerignore` excludes `.git`, and the final image stage copies only `/opt/venv` and
  `/app/src` (run with `PYTHONPATH=/app/src`). There is no `.git` in the image.
- The Docker build **never runs `stamp-buildinfo.sh`**, so no `_buildinfo.py` is baked.
- `release-image.yml` passes no commit/release provenance into the build.

Consequently, inside a published `ghcr.io/randomparity/kdive:0.3.0` container,
`python -m kdive --version` reports **`0.3.0-dev`** — no commit id, no release flag — because
`_from_baked()` returns `None` (no module) and `_from_git()` returns `None` (git is installed
but `/app` has no repo). This contradicts the documented `releasing.md` contract ("The SHA/flag
come from a baked `_buildinfo.py` **in artifacts** …") and defeats provenance for the exact
artifact operators run in production. The image's OCI labels carry the revision (via
`docker/metadata-action`), but the running binary's self-reported version does not.

## Goal

The published container image reports the same self-describing version as the wheel:

- an image built from a `vX.Y.Z` tag reports `X.Y.Z+g<sha>`;
- an `:edge` / main-branch image reports `X.Y.Z-dev+g<sha>`;

where `<sha>` is the short commit the image was built from — the same commit prefix the wheel
reports for that tag.

The short SHA is pinned to a fixed abbreviation length so it renders the same across build
environments. Git's default `--short` auto-grows the abbreviation to stay unambiguous across the
objects present, so a **shallow** clone (`release-image.yml`) and a **full** clone (`release.yml`)
could abbreviate the same commit to different lengths. Both paths use `git rev-parse --short=12
HEAD`; `--short=N` is a minimum length that git extends only on a hex-prefix collision — negligibly
unlikely at 12 hex in this repo — so in practice the wheel and image render the identical string,
and always the same commit prefix.

Out of scope: the operator-performed `v0.3.0` tag event itself; the post-release
`begin <next>-dev` bump; any change to the `full_version()` rendering rules or the wheel/sdist
path (both already correct).

## Approach

Bake `_buildinfo.py` during the Docker build from **build arguments**, reusing the existing
`stamp-buildinfo.sh` as the single source of the file's format. A hermetic container build has
no `.git` (intentionally — excluding it keeps the context small and cache stable), so the commit
and release flag are conveyed in from the workflow, which knows the ref.

1. **`scripts/stamp-buildinfo.sh`** — accept an explicit commit override via the
   `KDIVE_BUILDINFO_COMMIT` environment variable, falling back to git when it is unset. Pin the
   git-derived abbreviation to `git rev-parse --short=12 HEAD` (was `--short`, auto length) so the
   stamped SHA is deterministic across shallow and full clones. The `RELEASE` arg (`$1`) is
   unchanged. This keeps one script and one `_buildinfo.py` format for both the wheel path
   (git-derived) and the container path (arg-derived); the only change to existing callers is the
   now-fixed SHA width.

2. **`Dockerfile`** — in the *builder* stage (which already has `scripts/` and `src/` via
   `COPY . .`), after the project sync, declare `ARG KDIVE_COMMIT` and `ARG KDIVE_RELEASE=false`
   and run the stamp only when `KDIVE_COMMIT` is non-empty:

   ```dockerfile
   ARG KDIVE_COMMIT=""
   ARG KDIVE_RELEASE="false"
   RUN if [ -n "$KDIVE_COMMIT" ]; then \
         KDIVE_BUILDINFO_COMMIT="$KDIVE_COMMIT" ./scripts/stamp-buildinfo.sh "$KDIVE_RELEASE"; \
       fi
   ```

   The file lands at `/app/src/kdive/_buildinfo.py`, which the final stage carries via its
   existing `COPY --from=builder /app/src /app/src`. Runtime import resolves it through
   `PYTHONPATH=/app/src`. The `ARG`s are placed *after* the dependency-sync layers so a changing
   commit never busts the expensive `uv sync` cache. When no build-arg is supplied (a local
   `docker build`, the ci.yml PR build), the stamp is skipped and the image behaves exactly as
   today — no silent `unknown` provenance.

3. **`.dockerignore`** — add `src/kdive/_buildinfo.py`, so a developer's leftover local
   `_buildinfo.py` (from a prior `just build`, before its cleanup trap) can never be copied by
   `COPY . .` and baked stale into an image. Provenance then comes only from the explicit,
   authoritative build-arg path.

4. **`release-image.yml`** — resolve the short SHA once and pass both build-args to
   `docker/build-push-action`:

   ```yaml
   build-args: |
     KDIVE_COMMIT=<git rev-parse --short=12 HEAD>
     KDIVE_RELEASE=${{ startsWith(github.ref, 'refs/tags/v') }}
   ```

   This covers both triggers the workflow already has: a `main` push (`RELEASE=false` →
   `-dev+g<sha>`) and a `vX.Y.Z` tag (`RELEASE=true` → `+g<sha>`). No change to `ci.yml`'s
   push-less PR build is required or wanted.

## Alternatives considered

- **Un-ignore `.git` and let live-git resolution work in the container.** Rejected: ships the
  whole history in every layer, bloats the image, and busts build cache on every commit. The
  final stage would still need `.git` copied in, defeating the slim runtime base.
- **Duplicate the `_buildinfo.py` heredoc directly in the Dockerfile.** Rejected: two sources of
  the file's format drift; reusing `stamp-buildinfo.sh` keeps one.
- **Carry provenance only via OCI labels (`org.opencontainers.image.revision`).** Rejected: it
  is already present and does not fix the problem — an operator running `--version` inside the
  container, or reading the startup log, still sees `0.3.0-dev`. The documented contract is that
  the *binary* reports its own provenance.
- **Bake at the wheel and `pip install` the wheel into the image.** Rejected as a larger
  refactor of the image build (which syncs from source via `uv`, not a wheel) for no additional
  provenance benefit over the build-arg stamp.

## Verification

- **Behavior** — a test drives `stamp-buildinfo.sh` with an explicit `KDIVE_BUILDINFO_COMMIT`
  and each `RELEASE` value in a throwaway tree, then asserts the generated `_buildinfo.py`
  drives `full_version()` to `X.Y.Z+g<sha>` (release) and `X.Y.Z-dev+g<sha>` (dev). This
  exercises the exact code path the container uses, minus the container.
- **End-to-end (CI image smoke)** — CI already builds `kdive:ci` (amd64, `load: true`) on every
  PR and runs `tests/image/` over it via `docker run` (`ci.yml`). Extend that build to pass
  `KDIVE_COMMIT`/`KDIVE_RELEASE` (RELEASE=false for a PR) and add a `tests/image/` case asserting
  `docker run kdive:ci python -m kdive --version` matches `^kdive X.Y.Z-dev\+g[0-9a-f]{12}$`. This
  exercises the real multi-stage `COPY` and the `PYTHONPATH` import in the actual image — catching
  a broken final-stage copy or a lost `_buildinfo.py` that the structural guard below cannot see.
- **Wiring (cheap regression guard)** — a structural test parses the `Dockerfile` and
  `release-image.yml` and asserts the build-arg stamp step exists and the workflow passes
  `KDIVE_COMMIT` + `KDIVE_RELEASE`. Belt-and-suspenders over the end-to-end smoke: it fails fast,
  without a build, if either end of the wiring is deleted.
- **Backward compatibility** — the existing `stamp-buildinfo.sh` git-derived path and all
  `tests/test_version.py` cases stay green (the env override is additive).
- Guardrails: `just lint`, `just type`, `just lint-shell`, `just test` (whole-tree), plus the
  doc guards CI gates individually.

## Acceptance

- A container image built from a `vX.Y.Z` tag reports `X.Y.Z+g<sha>` from `python -m kdive
  --version` and the startup log; an `:edge` image reports `X.Y.Z-dev+g<sha>`.
- The CI image smoke test asserts the built PR image reports `X.Y.Z-dev+g<12 hex>`.
- The wheel and the tag image report the same commit prefix `<sha>` for the same tag.
- `stamp-buildinfo.sh` keeps its current git-derived behavior when `KDIVE_BUILDINFO_COMMIT` is
  unset.
- `just ci` green, including the new behavior + wiring tests.
