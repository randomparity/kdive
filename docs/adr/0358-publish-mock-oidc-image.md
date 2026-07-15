# ADR 0358 — Publish the multi-arch mock OIDC mirror to GHCR and digest-pin it in compose

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** kdive maintainers
- **Issue:** #1184
- **Epic:** #1189 (cross-platform dev tooling)
- **Related:** ADR-0356 (arch-support matrix + drift guard), ADR-0357 (the in-repo mirror
  Dockerfile), ADR-0359 (multi-arch app-image publish — the sibling publish pipeline)

## Context

ADR-0357 (#1183) made the mock OIDC issuer buildable on ppc64le by building it in-repo
(`deploy/mock-oidc`) and flipping the compose `oidc` service to `build: ./deploy/mock-oidc`.
That closed the *build* gap but left every developer building the JVM image locally on first
`docker compose up` — a Maven resolve of ~40 jars plus a JRE base pull — on every machine.

Epic #1189's next step (#1184) is to publish that mirror as a `linux/amd64,linux/ppc64le`
manifest on GHCR so onboarding *pulls* a prebuilt image, and to pin it by digest so the
reference is immutable (the ADR-0356 guard already forbids floating-tag drift for the images
it tracks). The upstream `ghcr.io/navikt/mock-oauth2-server` publishes only amd64/arm64, which
is why the mirror exists; the published mirror carries the two architectures kdive's core loop
targets (amd64 + ppc64le).

Two constraints shape the design:

- **The mirror image runs no target-arch code at build time.** The builder stage is
  `$BUILDPLATFORM`-pinned and the runtime stage only `COPY`s the arch-neutral jars onto a
  per-arch JRE base (ADR-0357). So the ppc64le layer assembles on an amd64 runner with no QEMU
  — unlike the app image (ADR-0359), which compiles ppc64le C/C++ deps from source under
  emulation.
- **The CI compose smoke brings up `oidc`.** `tests/image/test_compose_smoke.py` runs
  `docker compose up --wait server`, and `server` depends on `oidc`, so whatever the `oidc`
  reference resolves to must succeed on an unauthenticated GitHub-hosted runner.

## Decision

1. **Add `.github/workflows/publish-mock-oidc.yml`.** It builds `deploy/mock-oidc` for
   `linux/amd64,linux/ppc64le` with buildx and pushes to
   `ghcr.io/randomparity/mock-oauth2-server`, tagged `<version>` and `<version>-<short-sha>`
   (the version read from the Dockerfile `MOCK_OAUTH2_SERVER_VERSION` ARG, the single pin, so
   the tag never drifts from the resolved jar). It authenticates with the workflow
   `GITHUB_TOKEN` scoped to `packages: write` (no long-lived PAT), and a final step runs
   `docker buildx imagetools inspect` on the pushed digest and **fails** unless the manifest
   lists both `linux/amd64` and `linux/ppc64le`, so a base image that silently dropped an arch
   cannot publish green. No `setup-qemu-action`: no target-arch code executes at build.

   The workflow triggers on push to `main` filtered to `paths: [deploy/mock-oidc/**]`, plus
   `workflow_dispatch`. That path filter is deliberate: a new manifest digest must be produced
   (and re-pinned) exactly when the mirror source changes, so tying the publish to that change
   means an unrelated push to `main` never republishes and moves the digest out from under the
   compose pin.

2. **Pin the published manifest by digest in compose, keep `build:` as a fallback.** The
   `oidc` service becomes
   `image: ghcr.io/randomparity/mock-oauth2-server@sha256:<digest>` while retaining
   `build: ./deploy/mock-oidc`. `docker compose up` pulls the digest when it is reachable and
   **falls back to building locally when the pull is denied** (registry offline, package
   private, or an arch the mirror does not publish — e.g. arm64). So the stack never
   hard-depends on the pull: onboarding gets the fast prebuilt path where available and the
   correct local build everywhere else, and the CI smoke stays green on an unauthenticated
   runner (the private-package pull is denied → it builds `oidc` locally, exactly as it did
   under ADR-0357).

3. **Record the image in the ADR-0356 matrix under a new `publish-mirror` handling token.** A
   kdive-published multi-arch mirror is neither `rely-on-upstream` (it is not someone else's
   image), nor `mirror` (that token marks an *incomplete* follow-up that only cites a tracking
   issue and asserts nothing about ppc64le), nor `build-local` (which records `—` arch cells
   because the image is built, not pulled per-arch — hiding the published coverage that is the
   whole point here). Overloading one of those with a prose note is exactly the anti-pattern
   ADR-0356's rejected-alternatives section fences off. `publish-mirror` carries its own
   machine-checked obligation in `scripts/check_container_arch_matrix.py`: the ppc64le cell is
   exactly `✅` (fail-closed, like `rely-on-upstream`) **and** the compose reference is pinned
   by an `@sha256:` digest. The matrix row is amd64 `✅` / arm64 `❌` / ppc64le `✅` — the mirror
   publishes the two core-loop arches; arm64 is served by the `build:` fallback.

The current digest was bootstrapped by publishing `deploy/mock-oidc` as it stands, and the
workflow owns every subsequent publish (it fires on the same `deploy/mock-oidc/**` change that
requires re-pinning).

## Consequences

- First `docker compose up` pulls a prebuilt OIDC image where the published mirror is reachable
  (amd64 + ppc64le), instead of building the JVM image on every machine; the local build
  remains the automatic fallback, so no host is worse off than under ADR-0357.
- The published manifest is verified multi-arch at publish time, giving the ADR-0356 guard's
  static ppc64le `✅` an actual build-and-inspect behind it for this row (the guard cannot probe
  a registry; the workflow does).
- The GHCR package must be **public** for unauthenticated onboarding to pull it (mirroring the
  already-public `ghcr.io/randomparity/kdive` app package). GitHub exposes no REST endpoint to
  flip a user package's visibility, so a maintainer sets it once in the GHCR UI; until then the
  `build:` fallback covers unauthenticated hosts, so nothing is blocked.
- arm64 dev hosts (e.g. Apple Silicon) are not in the published manifest and fall back to a
  local build. Adding arm64 to the publish matrix is a one-line change if that build cost
  becomes a burden; it is left out now because the epic targets amd64 + ppc64le.
- The bootstrapped digest and a later workflow rebuild can differ (buildkit is not guaranteed
  bit-reproducible across versions). The `@sha256:` pin stays valid regardless — it addresses
  an immutable manifest — and the pin is only ever advanced deliberately when `deploy/mock-oidc`
  changes and the workflow prints the new digest.
- The Helm demo (`deploy/helm/kdive/values.yaml`) can now repoint at this published mirror
  instead of the amd64/arm64-only upstream; that repoint stays an ADR-0356 follow-up.

## Alternatives considered

- **Keep `build:` only (no publish), as the app image does via `${KDIVE_IMAGE:-kdive:dev}`.**
  Rejected: it leaves every machine building the JVM image, which is the cost #1184 exists to
  remove. The app image legitimately stays build-only because its ppc64le build compiles from
  source under emulation (ADR-0359); the OIDC mirror is a cheap COPY-only build that publishes
  cleanly.
- **Pin a floating tag (`:3.0.3`) instead of the digest.** Rejected: a tag can be retargeted
  after a rebuild, so the compose reference would not be immutable; the ADR-0356 guard's
  `publish-mirror` obligation requires the `@sha256:` pin for this reason.
- **Drop the `build:` fallback once the image is published.** Rejected: it would make the stack
  hard-fail when the registry is unreachable or the package is private, and would break the CI
  smoke on the unauthenticated runner. The fallback also transparently serves arm64.
- **Reuse the `mirror` or `build-local` token rather than add `publish-mirror`.** Rejected:
  `mirror` asserts nothing about ppc64le and signals an unfinished follow-up; `build-local`
  records `—` arch cells and hides that the image is now pulled per-arch. Both would make a
  published, ppc64le-verified image indistinguishable from a different situation to the guard
  and a human reader — the overloading ADR-0356 explicitly rejects.
- **Add `setup-qemu-action` for parity with `release-image.yml`.** Rejected as dead weight: no
  target-arch code runs during this build, so emulation is never exercised; the workflow
  comment records why it is absent.
- **Trigger the publish on every push to `main`.** Rejected: it would republish (and move the
  digest) on changes unrelated to the mirror, thrashing the compose pin. Gating on
  `deploy/mock-oidc/**` ties a new digest to the change that requires re-pinning.
