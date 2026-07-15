# ADR 0358 — Publish the multi-arch mock OIDC mirror to GHCR; consume it via `KDIVE_OIDC_IMAGE`

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** kdive maintainers
- **Issue:** #1184
- **Epic:** #1189 (cross-platform dev tooling)
- **Related:** ADR-0356 (arch-support matrix + drift guard), ADR-0357 (the in-repo mirror
  Dockerfile), ADR-0359 (multi-arch app-image publish — the sibling `KDIVE_IMAGE` pattern)

## Context

ADR-0357 (#1183) made the mock OIDC issuer buildable on ppc64le by building it in-repo
(`deploy/mock-oidc`) and setting the compose `oidc` service to `build: ./deploy/mock-oidc`.
That closed the *build* gap but left every developer building the JVM image locally on first
`docker compose up` — a Maven resolve of ~40 jars plus a JRE base pull — on every machine.

Epic #1189's next step (#1184) is to publish that mirror as a `linux/amd64,linux/ppc64le`
manifest on GHCR so a developer can *pull* a prebuilt image, pinned by digest, instead of
building it.

Two facts learned while implementing this shape the decision:

- **A `@sha256:` digest `image:` cannot coexist with `build:`.** If a compose service pins a
  digest and also has a `build:` key, `docker compose up` pulls the digest when it is reachable
  but, when the pull fails, falls through to *building* — and buildkit then rejects the build
  with `invalid tag "…@sha256:…"`, because a locally-built image cannot be tagged to a
  caller-specified digest (the digest is derived from content, not assigned). So a hardcoded
  digest and a working local build are mutually exclusive: you get one path or the other, never
  a pull-with-build-fallback. (Observed directly: the CI compose smoke on an unauthenticated
  runner failed with exactly this error.)
- **The CI compose smoke brings up `oidc` unauthenticated.** `tests/image/test_compose_smoke.py`
  runs `docker compose up --wait server`, and `server` depends on `oidc`, on a GitHub-hosted
  runner with no GHCR login. A private published package is therefore not pullable there, so a
  committed hardcoded digest would force that job to either fail (private) or depend on the
  package being public forever.

## Decision

1. **Add `.github/workflows/publish-mock-oidc.yml`.** It builds `deploy/mock-oidc` for
   `linux/amd64,linux/ppc64le` with buildx and pushes to
   `ghcr.io/randomparity/mock-oauth2-server`, tagged `<version>` and `<version>-<short-sha>`
   (the version read from the Dockerfile `MOCK_OAUTH2_SERVER_VERSION` ARG — the single pin — so
   the tag never drifts from the resolved jar). It authenticates with the workflow
   `GITHUB_TOKEN` scoped to `packages: write` (no long-lived PAT), and a final step runs
   `docker buildx imagetools inspect` on the pushed digest and **fails** unless the manifest
   lists both `linux/amd64` and `linux/ppc64le`, so a base image that silently dropped an arch
   cannot publish green. No `setup-qemu-action`: the builder stage is `$BUILDPLATFORM`-pinned
   and the runtime stage only `COPY`s the arch-neutral jars, so no target-arch code executes at
   build (ADR-0357) — contrast `release-image.yml`, whose app build compiles ppc64le deps from
   source and therefore does need QEMU (ADR-0359). The workflow triggers on push to `main`
   filtered to `paths: [deploy/mock-oidc/**]`, plus `workflow_dispatch`, so a new digest is
   produced exactly when the mirror source changes and an unrelated push never republishes.

2. **Consume the published mirror through `KDIVE_OIDC_IMAGE`, defaulting to the local build.**
   The compose `oidc` service is `image: ${KDIVE_OIDC_IMAGE:-kdive-mock-oidc:dev}` with
   `build: ./deploy/mock-oidc`. Unset (the default and CI), `build:` builds the mirror locally
   and tags it `kdive-mock-oidc:dev` — `docker compose up` works offline and on any arch whose
   bases publish, unchanged from ADR-0357. Set to the published digest
   (`ghcr.io/randomparity/mock-oauth2-server@sha256:<digest>`), compose pulls that immutable,
   digest-pinned manifest instead of building. This is exactly the `${KDIVE_IMAGE:-kdive:dev}`
   pattern ADR-0359 uses for the app image: the published, digest-pinned artifact is the
   *override*, and the local build is the default, because the two cannot be one hardcoded
   reference (fact 1 above).

Because the compose reference resolves to `kdive-mock-oidc:dev` with the variable unset, the
ADR-0356 arch-matrix row stays `build-local` (the guard reads the default): no new handling
token or matrix flip is needed, and the drift guard keeps passing.

## Consequences

- A developer who exports `KDIVE_OIDC_IMAGE=<digest>` pulls a prebuilt amd64/ppc64le image
  instead of building the JVM image; everyone else keeps the working local build, so no host is
  worse off than under ADR-0357, offline included.
- The published manifest is verified multi-arch at publish time (the workflow's `imagetools`
  check), and `docker compose up` with `KDIVE_OIDC_IMAGE` set to the digest was confirmed to
  pull it and serve OIDC discovery.
- The digest is not hardcoded in `docker-compose.yml`, so the compose file needs no edit on each
  republish; the workflow prints the current digest in its run summary and the `deploy/mock-oidc`
  README documents exporting it. The tradeoff is that the fast pull path is opt-in rather than
  the default — accepted because a hardcoded digest cannot keep the local build working (fact 1)
  and would break the unauthenticated CI smoke (fact 2).
- For an unauthenticated `docker pull` to succeed, the GHCR package must be **public** (like the
  already-public `ghcr.io/randomparity/kdive` app package). GitHub exposes no REST endpoint to
  flip a user package's visibility, so a maintainer sets it once in the GHCR UI; until then an
  authenticated pull (or the default local build) works.
- arm64 dev hosts are not in the published manifest (the epic targets amd64 + ppc64le); they use
  the default local build. Adding arm64 to the publish matrix is a one-line change if wanted.
- The Helm demo (`deploy/helm/kdive/values.yaml`) can now repoint at this published mirror
  instead of the amd64/arm64-only upstream; that repoint stays an ADR-0356 follow-up.

## Alternatives considered

- **Hardcode the digest in compose with `build:` as a fallback.** Rejected — mechanically
  impossible: `image: <digest>` + `build:` fails the build path with `invalid tag` (fact 1), and
  the unauthenticated CI smoke takes that path. Proven by a red CI run before this decision.
- **Hardcode the digest with no `build:` (pure pull).** Rejected: it drops the offline/local
  build entirely and makes the CI smoke (and every host) depend on the package being public and
  reachable, coupling the app-image smoke to a GHCR pull of a backend. The `KDIVE_OIDC_IMAGE`
  override delivers the pull path without giving up the local build.
- **Log the CI smoke job into GHCR and keep a hardcoded private digest.** Rejected: it couples an
  app-image test to a backend registry credential and still loses the offline build; the
  env-override needs no CI change at all.
- **Add `setup-qemu-action` for parity with `release-image.yml`.** Rejected as dead weight: no
  target-arch code runs during this build, so emulation is never exercised; the workflow comment
  records why it is absent.
- **Trigger the publish on every push to `main`.** Rejected: it would republish on changes
  unrelated to the mirror. Gating on `deploy/mock-oidc/**` ties a new digest to the change that
  produces it.
