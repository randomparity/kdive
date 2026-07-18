# In-repo, multi-arch mock OIDC issuer for the developer stack (#1183, ADR-0357).
#
# The upstream image (ghcr.io/navikt/mock-oauth2-server) is amd64/arm64 only, so
# `docker compose up` breaks on a ppc64le POWER host. mock-oauth2-server is pure JVM
# bytecode, so portability needs only a multi-arch JRE base — no Kotlin rebuild. Both
# base images below publish ppc64le (and amd64/arm64), so `docker build` on any of those
# hosts produces a native image for that arch with no registry mirror.
#
# The runnable jar is NOT published to a public URL for 3.0.3 (Maven Central ships only the
# thin library jar; the fat jar lives in auth-gated GitHub Packages). So the builder stage
# resolves the exact runtime closure from Maven Central — which verifies each artifact's
# published checksum on download — and we additionally pin the primary jar's sha256 below.
# Bases are pinned by their multi-arch index digest so a per-arch pull still resolves.

# --- builder: resolve the runtime classpath from Maven Central --------------------------
# Pinned to $BUILDPLATFORM (the native build host) on purpose: the resolved artifacts are
# arch-neutral JVM bytecode, so a cross-arch `buildx --platform=linux/ppc64le` build resolves
# them natively on the amd64 CI runner instead of running Maven's JVM under qemu-user
# emulation — which segfaults for ppc64le (ADR-0355). The runtime stage below is the only
# arch-specific layer, and it merely COPYs jars, so no target-arch code executes at build.
FROM --platform=$BUILDPLATFORM maven:3-eclipse-temurin-26@sha256:d5617b9a6307e1b51dc7c55edf09bacb66f1c91fb861949c34a3a0d4e16bd241 AS builder

# The one version pin that drives the image; keep in lockstep with pom.xml.
ARG MOCK_OAUTH2_SERVER_VERSION=3.0.3
# sha256 of mock-oauth2-server-<version>.jar as published on Maven Central.
ARG MOCK_OAUTH2_SERVER_SHA256=20cc581a487862292af9af9625afff98d747a9ce9240d18404589304acbbab86

WORKDIR /build
COPY pom.xml ./
# Copy the dependency (mock-oauth2-server) plus its full runtime transitive closure into
# lib/. -o is impossible (nothing cached), but --strict-checksums makes a corrupted download
# fail the build instead of silently proceeding.
RUN mvn --batch-mode --strict-checksums \
        dependency:copy-dependencies \
        -DincludeScope=runtime \
        -DoutputDirectory=/build/lib \
 && echo "${MOCK_OAUTH2_SERVER_SHA256}  /build/lib/mock-oauth2-server-${MOCK_OAUTH2_SERVER_VERSION}.jar" \
        | sha256sum --check --strict

# --- runtime: the resolved classpath on a bare multi-arch JRE ---------------------------
FROM eclipse-temurin:25-jre@sha256:d0eb1b9018b3044da1b7346f39e945f71095749853d69a3aa16b8c99dad9bb45

# Reproduce the compose service contract: the standalone main reads SERVER_PORT (default
# 8080) and serves the `/default` issuer's discovery/JWKS/token endpoints. aud=kdive is
# derived per request (the token's aud falls back to the request's scope/audience), exactly
# as the retired upstream image did — no JSON_CONFIG is needed for the compose flow.
ENV SERVER_PORT=8080
EXPOSE 8080

WORKDIR /app
COPY --from=builder /build/lib/ /app/lib/

# Run the published standalone entrypoint class off the resolved classpath. Keeping the jars
# separate (vs. an uber-jar) preserves each dependency's META-INF/services and native bits.
USER 1000
ENTRYPOINT ["java", "-cp", "/app/lib/*", "no.nav.security.mock.oauth2.StandaloneMockOAuth2ServerKt"]
