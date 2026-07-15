# Multi-arch mock OIDC issuer

An in-repo Docker build of [`mock-oauth2-server`](https://github.com/navikt/mock-oauth2-server)
for the developer compose stack. It stands in for a real OpenID Connect issuer so the kdive
`server` validates real signed bearer tokens locally — see the repo-root `docker-compose.yml`
`oidc` service and `examples/local-libvirt/mint-token.sh`.

## Why this exists

The upstream image `ghcr.io/navikt/mock-oauth2-server` publishes only `amd64`/`arm64`
manifests, so `docker compose up` fails on a ppc64le POWER host. `mock-oauth2-server` is pure
JVM bytecode, so portability needs only a multi-arch JRE base — no Kotlin rebuild. This build
runs natively on any architecture whose base images publish (amd64, arm64, ppc64le). See
ADR-0356 (arch-support matrix) and ADR-0357 (this build).

## How the jar is pinned

There is no public, no-auth URL for a runnable standalone jar of version 3.0.3 (the GitHub
release has no jar asset, GitHub Packages is auth-gated, and Maven Central ships only the
non-runnable thin library jar). So the build resolves the runtime classpath from Maven Central
instead:

- `pom.xml` pins the single dependency `no.nav.security:mock-oauth2-server:3.0.3`.
- The **builder** stage runs `mvn dependency:copy-dependencies` to copy that jar plus its full
  runtime transitive closure into `lib/`. Maven verifies every artifact's published checksum on
  download (`--strict-checksums`); the Dockerfile additionally pins the primary jar's `sha256`.
- The builder is pinned to `$BUILDPLATFORM` (the native build host). The resolved jars are
  arch-neutral, so a `buildx --platform=linux/ppc64le` cross-build resolves them on the amd64
  runner rather than running Maven's JVM under qemu-user emulation (which segfaults for ppc64le;
  ADR-0355). The runtime stage — the only arch-specific layer — merely copies the jars.
- Both base images are pinned by their multi-arch **index** digest so a per-arch pull still
  resolves.

## Updating the mock issuer version

1. Bump the `<version>` in `pom.xml`.
2. Update `MOCK_OAUTH2_SERVER_VERSION` and `MOCK_OAUTH2_SERVER_SHA256` in the `Dockerfile`. Get
   the checksum from Maven Central, e.g.:

   ```
   curl -sL https://repo1.maven.org/maven2/no/nav/security/mock-oauth2-server/<version>/mock-oauth2-server-<version>.jar.sha256
   ```

3. To refresh a base image, re-pin its index digest:

   ```
   skopeo inspect --raw docker://eclipse-temurin:21-jre | sha256sum
   ```

## Configuration

The container reproduces the retired upstream service's config: it reads `SERVER_PORT`
(compose sets `8080`) and serves the `/default` issuer's discovery, JWKS, and token endpoints.
The `aud` claim is derived per request (falls back to the request scope/audience), so a token
minted with `scope=kdive` carries `aud=kdive` — byte-identical to the retired image because it
is the same jar version. No `JSON_CONFIG` is needed for the compose flow.

## Build and smoke-test standalone

```
docker build -t kdive-mock-oidc:dev deploy/mock-oidc
docker run --rm -p 8090:8080 kdive-mock-oidc:dev
curl -s http://localhost:8090/default/.well-known/openid-configuration
```
