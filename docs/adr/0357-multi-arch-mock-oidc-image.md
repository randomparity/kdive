# ADR 0357 — In-repo multi-arch mock OIDC image built from the upstream jar

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** kdive maintainers

## Context

The developer compose stack pulled its mock OIDC issuer from
`ghcr.io/navikt/mock-oauth2-server:3.0.3`, which publishes only `amd64`/`arm64` manifests. On
a ppc64le POWER host `docker compose up` therefore fails to start `oidc`, breaking the whole
stack (the `server` validates bearer tokens against it). ADR-0356 fenced this gap as the
`mirror` follow-up tracked by #1183; this ADR records how the mirror is actually built.

`mock-oauth2-server` is pure JVM bytecode, so cross-arch portability needs only a multi-arch
JRE base — no Kotlin rebuild (ADR-0356). `eclipse-temurin` publishes a ppc64le JRE (and JDK),
so a locally-built image runs natively on POWER.

The issue assumed a checksum-pinned standalone fat jar fetchable by a public URL. That premise
does not hold for 3.0.3: the GitHub release carries no jar asset, GitHub Packages requires
authentication (HTTP 401), and Maven Central ships only the **thin** library jar (no bundled
dependencies, not runnable on its own). A no-auth, reproducible build therefore has to assemble
the runtime classpath from Maven Central rather than download one prebuilt jar.

## Decision

We will build the mock OIDC issuer in-repo at `deploy/mock-oidc` with a two-stage Dockerfile,
and switch the compose `oidc` service from `image:` to `build: ./deploy/mock-oidc` (naming the
result `kdive-mock-oidc:dev`, mirroring the `kdive:dev` `build-local` pattern):

1. A **builder** stage (`maven:3.9-eclipse-temurin-21`, pinned to `$BUILDPLATFORM`) resolves
   the exact runtime closure of `no.nav.security:mock-oauth2-server:3.0.3` from Maven Central
   into `lib/` via `mvn dependency:copy-dependencies`. Maven verifies each artifact's published
   checksum on download (`--strict-checksums`); we additionally pin the primary jar's sha256.
   The stage is pinned to the native build host on purpose — the resolved jars are arch-neutral
   bytecode, so a `buildx --platform=linux/ppc64le` build resolves them on the amd64 CI runner
   instead of running Maven's JVM under qemu-user emulation, which segfaults for ppc64le
   (ADR-0355).
2. A **runtime** stage (`eclipse-temurin:21-jre`, the only arch-specific layer) copies `lib/`
   and runs the published standalone entrypoint class off the classpath:
   `java -cp "/app/lib/*" no.nav.security.mock.oauth2.StandaloneMockOAuth2ServerKt`. It sets
   `SERVER_PORT=8080`, reproducing the retired service's config exactly.

Both base images are pinned by their multi-arch **index** digest so a per-arch pull still
resolves. The single version pin (pom.xml + the sha256 in the Dockerfile) drives the image.

The arch-support matrix in ADR-0356 flips the OIDC row from `mirror` to `build-local`, and
`scripts/check_container_arch_matrix.py` enforces that the compose `oidc` service carries a
`build:` key.

## Consequences

- `docker compose up oidc` builds and serves discovery, JWKS, and the token endpoint on the
  `/default` issuer on any arch whose base images publish (amd64/arm64/ppc64le). Because it is
  the same jar version as the retired image, the token contract (`aud=kdive` derived from the
  request scope, `iss=…/default`) is byte-identical, so `mint-token.sh` and `scripts/live-stack`
  are unchanged.
- CI verification is a `buildx` ppc64le cross-build (ADR-0356 posture), which works without a
  POWER runner because no target-arch code executes during the build.
- The build reaches Maven Central at build time (a first `compose up` needs network), and the
  runtime image carries ~40 dependency jars instead of one uber-jar — deliberate, so each
  dependency keeps its own `META-INF/services` and native resources (no shade-merge pitfalls).
- Updating the mock issuer is a two-line change (the pom version + the Dockerfile sha256); the
  README documents it.
- The Helm demo (`deploy/helm/kdive/values.yaml`) still references the upstream image and stays
  amd64/arm64: a k8s deploy *pulls*, so it needs a published image, not a compose `build:`.
  That repoint remains an ADR-0356 follow-up (it depends on a publish pipeline, out of scope
  here).

## Alternatives considered

- **Download a prebuilt standalone jar by URL + digest** (the issue's stated approach).
  Rejected: no such public, no-auth URL exists for 3.0.3 (release has no jar asset; GitHub
  Packages is auth-gated; Maven Central's jar is the non-runnable thin library jar).
- **An uber-jar via `maven-shade-plugin`.** Rejected: shade must merge `META-INF/services`
  and handle native resources with the right transformers; a plain `copy-dependencies` +
  classpath keeps each jar intact and is simpler and less error-prone.
- **Rebuild from Kotlin source in the image.** Rejected: unnecessary (the published bytecode is
  arch-neutral) and it drags a full Gradle/Kotlin toolchain into the build.
- **Run the upstream amd64/arm64 image under qemu-user on POWER.** Rejected in ADR-0355/0356:
  the emulated JVM deadlocks/segfaults.
- **Build the ppc64le variant with the Maven stage running under emulation** (omit
  `$BUILDPLATFORM`). Rejected: the emulated JVM segfaults (ADR-0355), so the CI cross-build
  would fail; pinning the builder to the native build host sidesteps emulation entirely.
- **Hand-list every dependency jar as a checksummed URL in the Dockerfile.** Rejected: ~40
  transitive jars would have to be enumerated and re-pinned on every bump — a maintenance
  burden Maven's own checksum-verified resolution removes, driven by a single version pin.
