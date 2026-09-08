# Mock OIDC image maintenance

This directory builds the development stack's token issuer from the published
`no.nav.security:mock-oauth2-server` JVM dependency. It owns image maintenance and standalone
diagnostics. Use the [Compose operating guide](../compose/README.md) or
[host-stack runbook](../../docs/operating/runbooks/live-stack.md) for a running KDIVE stack;
use the [release guide](../../docs/development/releasing.md#mock-oidc-mirror-publishing) for
publication and image tags.

## Build inputs

- [pom.xml](pom.xml) pins the issuer dependency. Keep that dependency version in sync with
  `MOCK_OAUTH2_SERVER_VERSION` and its primary-jar SHA-256 in the [Dockerfile](Dockerfile).
- Maven resolves the runtime classpath with `dependency:copy-dependencies` and
  `--strict-checksums`; the build additionally checks the primary jar against the pinned SHA-256.
  Published checksums detect download corruption; they do not lock every transitive dependency
  to a repository-reviewed SHA-256.
- The builder runs on `$BUILDPLATFORM`. The runtime copies the architecture-neutral jars onto
  the target JRE, so cross-building this image does not execute the target JVM or rebuild Kotlin.
  Both `FROM` references pin image-index digests. A first build needs registry and Maven access
  unless all required inputs are cached.
- The runtime runs as UID 1000 and starts the standalone class with `/app/lib/*` on its
  classpath. `SERVER_PORT` defaults to 8080. Compose supplies the port and uses the `/default`
  issuer; its client token and issuer-URL rules are in the Compose guide.

## Using the image

Compose's default tag is `kdive-mock-oidc:dev`, built from this directory.
`KDIVE_OIDC_IMAGE` selects a prebuilt reference; obtaining it requires access to its registry.
The [host environment script](../../scripts/live-stack/env.sh) also selects a pinned mirror
when the variable is unset, the host reports `ppc64le`, and its device-tree model contains
`emulated by qemu`. An explicitly empty variable preserves the local-build path.

On that local path, `just stack-up` and `scripts/live-stack/up.sh` build only when the local tag
is absent. After editing the POM or Dockerfile, rebuild explicitly from the repository root:

```bash
KDIVE_OIDC_IMAGE= docker compose build oidc
```

Then restart through the operating guide for your deployment. Building an image does not replace
a running container. Published-image users select a new digest instead of rebuilding the old one.

## Updating the image

1. Update the issuer **dependency** version in `pom.xml`, not the POM project's version.
   Set `MOCK_OAUTH2_SERVER_VERSION` to the same value and obtain the matching jar's SHA-256
   from its Maven Central artifact metadata for `MOCK_OAUTH2_SERVER_SHA256`.
2. For a base-image update, inspect the intended builder or runtime tag's multi-platform index
   with `docker buildx imagetools inspect`, then update that `FROM` reference's tag and index
   digest together. Preserve the amd64 and ppc64le targets required by the publishing workflow.
3. Build and run the standalone checks below. Discovery alone does not verify KDIVE token
   acceptance; use the operating guide's authenticated MCP request for that integration check.
4. Follow the [publishing workflow](../../docs/development/releasing.md#mock-oidc-mirror-publishing)
   to publish and adopt the resulting digest.

## Standalone discovery check

From the repository root, with host port 8090 free:

```bash
docker build -t kdive-mock-oidc:dev deploy/mock-oidc
docker run --rm -p 127.0.0.1:8090:8080 kdive-mock-oidc:dev
```

The issuer runs in the foreground. Once its startup log reports readiness, use a second terminal:

```bash
curl --fail --show-error http://127.0.0.1:8090/default/.well-known/openid-configuration
```

Stop the foreground process with Ctrl-C; `--rm` removes its container. This is a development
issuer that can mint accepted demo tokens for any caller; keep it local and separate from a
production identity provider.
