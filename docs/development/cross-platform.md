# Cross-platform development (x86_64 and ppc64le)

KDIVE develops and runs on `x86_64` and on `ppc64le` (POWER9/POWER10). The dev
loop is the same page on both: install the host prerequisites, sync the venv,
bring up the compose stack. This guide records the two places the arches diverge —
a Rust toolchain requirement on `ppc64le`, and a handful of container images with
no `ppc64le` manifest — and points at the POWER host bring-up runbook for a real
KVM-HV box.

Everything here matches what the tooling actually enforces: `just check-deps` is
arch-aware ([ADR-0360](../adr/0360-arch-aware-rust-dep-check.md)), the compose
image set is fenced against an arch-support matrix
([ADR-0356](../adr/0356-cross-platform-dev-containers.md)), and the OIDC and app
images are built or published multi-arch
([ADR-0357](../adr/0357-multi-arch-mock-oidc-image.md),
[ADR-0358](../adr/0358-publish-mock-oidc-image.md),
[ADR-0359](../adr/0359-multiarch-app-image.md)).

## Host prerequisites

Both arches need the same base tools; `ppc64le` adds a Rust toolchain. Let
`just check-deps` report what is missing — it captures the host arch and only
requires Rust on arches with no prebuilt wheels, so it never raises a false Rust
requirement on `x86_64`.

### Both arches

- **`libvirt-dev` and `python3-dev`** system headers — `libvirt-python` has no
  wheels and compiles against both, on every arch. `uv sync` fails without them.
- **[`uv`](https://docs.astral.sh/uv/)** — the Python toolchain manager.
- **`just` and `prek`** — install before `just setup`, which cannot bootstrap its
  own runner: `uv tool install rust-just prek`.

### ppc64le only

PyPI publishes no `ppc64le` wheels for `pydantic-core`, and `just`/`prek` install
from source too, so a **Rust toolchain must be on `PATH` first**
([rustup](https://rustup.rs)):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

`just check-deps` requires `rustc` **and** `cargo` on a wheel-less arch and prints
this exact rustup hint if either is missing
([ADR-0360](../adr/0360-arch-aware-rust-dep-check.md)); on `x86_64` it requires
neither. The runtime app image needs no Rust on either arch — `pydantic-core`
ships a `ppc64le` wheel for the pinned CPython, so only the dev `uv sync` (which
also builds the `just`/`prek` tools) pulls in the toolchain.

### The rest of setup is identical

```bash
uv tool install rust-just prek
just setup   # check host deps, sync the locked venv, install and run git hooks
```

See [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the full development loop and
[`docs/operating/install.md`](../operating/install.md) for the host-prerequisite
list. On a from-scratch POWER host, the
[POWER host bring-up runbook](../operating/runbooks/power-host-bringup.md) lists
the exact `apt-get` sets (including the `drgn`/kdump build headers and the
`live`-group source builds).

## Container images

`docker compose up` (the backend stack plus the app tier — see
[`docker-compose.yml`](../../docker-compose.yml)) draws on the images below. Most
publish `ppc64le` manifests and pull directly; two do not and are handled
explicitly. The full arch-support matrix, and the CI guard that fences the compose
file against it, live in [ADR-0356](../adr/0356-cross-platform-dev-containers.md).

| Image | ppc64le | How it is used |
|---|---|---|
| `postgres:17` | pulls | upstream multi-arch |
| `minio/minio`, `minio/mc` | pulls | upstream multi-arch |
| `prom/prometheus` | pulls | upstream multi-arch (`obs` profile) |
| `grafana/grafana` | **no image** | opt-in `obs` profile only; dashboards unavailable on ppc64le |
| mock OIDC issuer | builds/pulls | in-repo mirror (`deploy/mock-oidc`) — see below |
| `kdive` app image | builds/pulls | repo `Dockerfile`, published multi-arch — see below |

### The mock OIDC issuer

The upstream `ghcr.io/navikt/mock-oauth2-server` publishes only `amd64`/`arm64`,
so kdive mirrors it in-repo at `deploy/mock-oidc` on a multi-arch JRE base — the
same jar version, byte-identical token contract, no Kotlin rebuild
([ADR-0357](../adr/0357-multi-arch-mock-oidc-image.md), and the
[mirror README](../../deploy/mock-oidc/README.md)). The compose `oidc` service is:

```yaml
image: ${KDIVE_OIDC_IMAGE:-kdive-mock-oidc:dev}
build: ./deploy/mock-oidc
```

- **`KDIVE_OIDC_IMAGE` unset (default)** — `build:` builds the mirror locally and
  tags it `kdive-mock-oidc:dev`. Works offline and on any arch whose base images
  publish (`amd64`/`arm64`/`ppc64le`). This is the working default on POWER today.
- **`KDIVE_OIDC_IMAGE` set to the published digest** — compose pulls the prebuilt
  `linux/amd64,linux/ppc64le` mirror instead of building it:

  ```bash
  export KDIVE_OIDC_IMAGE=ghcr.io/randomparity/mock-oauth2-server@sha256:<digest>
  ```

  The [`publish-mock-oidc`](../../deploy/mock-oidc/README.md) workflow prints the
  current digest in its run summary. A digest reference cannot also be a `build:`
  target, so the pull is the override and the local build is the default — never
  both ([ADR-0358](../adr/0358-publish-mock-oidc-image.md)).

> The GHCR mirror package may still be **private**. Until a maintainer flips it
> public in the GHCR package settings, an unauthenticated `docker pull` of the
> digest fails — use the default local build (leave `KDIVE_OIDC_IMAGE` unset) or
> an authenticated pull meanwhile.

### The kdive app image

`ghcr.io/randomparity/kdive` is published as a `linux/amd64,linux/ppc64le`
manifest from the release path
([ADR-0359](../adr/0359-multiarch-app-image.md)). The `Dockerfile` guards its
`ppc64le` source-build steps (`grpcio` against system OpenSSL/zlib, the
autotools `drgn` build with `libkdumpfile`) behind `TARGETARCH`, so the `amd64`
image is unchanged. A POWER operator can `docker pull` the app tier rather than
build it. As with the OIDC mirror, `KDIVE_IMAGE` overrides the compose tag
(`${KDIVE_IMAGE:-kdive:dev}`) to drive a pre-built image; unset, `build: .` builds
`kdive:dev` from source and local dev is unchanged.

## Stack bring-up on a POWER host

For a clean `ppc64le` KVM-HV box — from OS install through a running local-libvirt
provider and the full kdive spine — follow the
[POWER host bring-up runbook](../operating/runbooks/power-host-bringup.md). It is
POWER-generic (POWER9 or POWER10) and its exit criterion is a single
`scripts/check-local-libvirt.sh` invocation that names the fix for each gap.

Two notes where the compose flow now differs from a hand-run stack:

- **OIDC.** The runbook's manual native-JVM OIDC step predates the in-repo mirror.
  With the mirror, `docker compose up` builds the OIDC issuer locally on `ppc64le`
  ([ADR-0357](../adr/0357-multi-arch-mock-oidc-image.md)), so that manual
  workaround is not needed for the compose flow.
- **Observability.** Skip the `obs` profile on `ppc64le` — Grafana has no
  `ppc64le` image. Prometheus does publish one, so run it alone if you need
  metrics:

  ```bash
  docker compose up -d prometheus
  ```

## Known gaps and slow paths

- **Grafana on ppc64le.** No upstream `ppc64le` image at any tag, so the opt-in
  `obs` dashboard is unavailable on POWER. Prometheus (the metrics store) is
  unaffected. Accepted gap, tracked in
  [ADR-0356](../adr/0356-cross-platform-dev-containers.md).
- **Emulated ppc64le app-image build is slow.** The release job builds the
  `ppc64le` leg under QEMU emulation on an `amd64` runner, where `grpcio` and
  `drgn` compile from source (minutes, not seconds). This is bounded to
  `main` pushes and release tags — PR CI stays `amd64`-only — and buildx layer
  caching amortizes it across runs
  ([ADR-0359](../adr/0359-multiarch-app-image.md)).
- **GHCR OIDC mirror visibility.** Documented above: pull requires the package to
  be public; the local build works regardless.
- **CI verification is buildx, not a POWER runner.** The epic proves the images
  *build* for `ppc64le` under `docker buildx`; runtime validation on real POWER is
  the separate live-hardware track
  ([ADR-0355](../adr/0355-power-native-kvm-hv-validation.md), and the runbook).
