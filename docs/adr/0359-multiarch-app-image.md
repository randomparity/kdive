# ADR 0359 — Multi-arch kdive application image (amd64 + ppc64le)

- **Status:** Accepted
- **Date:** 2026-07-15
- **Issue:** #1185
- **Epic:** #1189 (cross-platform dev tooling)
- **Related:** ADR-0356 (#1182 — cross-platform dev-container strategy; records that the
  buildx multi-arch build proving this row is a later epic sub-issue, #1185, and that the
  OIDC mock has no ppc64le image, mirror tracked #1183); ADR-0355 (#1156 — native POWER
  validation; records that a source-built drgn needs `libkdumpfile-dev` or kdump capture
  fails); ADR-0088 (the single control-plane image and its release/sign pipeline)

## Context

`release-image.yml` publishes `ghcr.io/randomparity/kdive` from the repo `Dockerfile` on a
push to `main` and on a `v*.*.*` tag. It built the runner's host arch only (amd64): no
`platforms:` key, no QEMU. A POWER operator therefore had to build the app tier from source
rather than pull it. ADR-0356 fixed the container arch strategy for the epic — *verification
posture is multi-arch image builds in CI (buildx), not a POWER test-runner* — and left the
`kdive` row as `build-local`, its ppc64le build "buildx-proven in #1185". This ADR is that
proof and the wiring that publishes it.

Two properties of the build shape the decision:

- **The `Dockerfile` was only ever exercised on amd64.** The builder stage installs
  `gcc libc6-dev libvirt-dev pkg-config`, which is exactly the amd64 build set: on amd64
  every locked dependency except `libvirt-python` resolves to a prebuilt wheel, so nothing
  else compiles. On ppc64le the wheel coverage is different and several deps build from
  source.
- **`astral-sh/uv` publishes no ppc64le container image.** The `Dockerfile` obtains `uv` via
  `COPY --from=ghcr.io/astral-sh/uv`, whose index carries only `linux/amd64` and
  `linux/arm64`. A `--platform linux/ppc64le` build fails at that COPY before any dependency
  is even resolved.

We inspected the lockfile to see what actually builds from source on ppc64le for the pinned
CPython 3.14 (`cp314`), rather than inheriting the issue's assumption. The finding corrects
that assumption: **`pydantic-core` publishes a `cp314` ppc64le wheel** (`2.46.4`), so it does
*not* build from source and **no Rust toolchain is required**. The deps with no ppc64le wheel
that do build from source are all C/C++:

| dependency | language | extra build need on ppc64le |
|---|---|---|
| `libvirt-python` | C | already covered (`gcc`, `libvirt-dev`, `pkg-config`) |
| `grpcio` | C++ | `g++` |
| `drgn` | C (autotools libdrgn) | `libelf-dev`, `libdw-dev`, `libkdumpfile-dev`, `autoconf`/`automake`/`libtool`/`autoconf-archive`, `make`, `gawk` |
| `pyyaml` | C ext | covered (`gcc`) |
| `markupsafe` | C ext | covered (`gcc`) |

`drgn` is the app's kdump/vmcore introspection tool. Its amd64 wheel vendors `libkdumpfile`;
a from-source `drgn` instead links the *system* library, and ADR-0355 recorded from native
POWER that without `libkdumpfile-dev` at build time "kdump capture fails" silently. So
`libkdumpfile-dev` (build) plus the runtime `libkdumpfile10` (final image) are hard
requirements on ppc64le, not optional features — omitting them would ship a ppc64le image
whose core function is quietly broken while `drgn --version` still passes.

## Decision

We will publish `ghcr.io/randomparity/kdive` as a `linux/amd64,linux/ppc64le` manifest from
the existing release path, harden the `Dockerfile` for the ppc64le source-build path with
arch-guarded package sets that leave the amd64 image byte-identical, and add a compose smoke
that drives the built image through `migrate` to a healthy server `/readyz`.

1. **Multi-arch publish on the release path only.** `release-image.yml` gains
   `docker/setup-qemu-action` and `platforms: linux/amd64,linux/ppc64le` on the buildx
   publish. The job already runs on `push: main` and `v*` tags; it does **not** run on
   `pull_request`. The multi-arch build is *not* added to PR CI (`ci.yml` stays amd64-only,
   `image build + smoke`), because the emulated ppc64le leg is slow (see the tradeoff below).
   `cosign` signs the returned index digest, so the whole manifest is signed.

2. **No Rust; arch-guarded C/C++ build deps.** The builder stage installs its extra
   ppc64le deps inside an `[ "$TARGETARCH" = "ppc64le" ]` guard, so the amd64 layer's package
   set — and thus the amd64 image size and contents — is unchanged. `pydantic-core`'s ppc64le
   wheel means no Rust toolchain is added anywhere. The final stage adds `libkdumpfile10`
   under the same guard so the source-built `drgn` can load its kdump support at runtime.

3. **Per-arch `uv` provider stage.** A small stage selector `FROM uv-${TARGETARCH} AS uv`
   resolves `uv`: amd64/arm64 copy from the pinned upstream image exactly as before (their
   `/uv` bytes are unchanged); ppc64le installs the pinned `uv` **wheel** from PyPI onto the
   python base and exposes it at the same `/uv`. BuildKit prunes the unselected provider
   stages, so an amd64 build never touches the ppc64le path and a ppc64le build never resolves
   the ppc64le-less upstream `uv` image. arm64 is preserved (not regressed) though the release
   job does not build it.

4. **Runtime `/readyz` proof runs on the native arch; ppc64le runtime proof is deferred.**
   `tests/image/test_compose_smoke.py` brings up the repo `docker-compose.yml` app tier
   against real Postgres/MinIO/OIDC backends and waits for the server healthcheck, which polls
   `/readyz` — a green `docker compose up --wait server` attests *migrate completed, then
   server reached `/readyz`* on `$KDIVE_IMAGE`. The compose image tag is now
   `${KDIVE_IMAGE:-kdive:dev}` so the smoke drives a pre-built image without rebuilding; unset,
   local dev is unchanged. This smoke runs in the amd64 `image build + smoke` job. It cannot
   run for ppc64le in CI: the server's `/readyz` gates on the OIDC mock, which has no ppc64le
   image (ADR-0356, mirror tracked #1183). Until that mirror lands, **ppc64le is gated by the
   buildx build-proof** (the image must build, and the build-time `drgn --version` / toolchain
   RUN must pass); the ppc64le runtime `/readyz` proof is a follow-up on #1183, and real-POWER
   runtime is the separate live-hardware track (ADR-0355).

### Emulated-build-time tradeoff

The ppc64le layer builds under QEMU user emulation on an amd64 runner. `grpcio` and `drgn`
compile from source under emulation, which is the slow leg of the release job (minutes, not
seconds — an amd64-only build compiled nothing). We accept this cost because the job runs only
on `main` pushes and release tags, not per-PR, and buildx layer caching amortizes it across
release runs. The alternatives were weighed and recorded rather than silently dropping the
arch:

- **Native ppc64le runner.** Removes emulation and would also unblock a native ppc64le runtime
  smoke, but GitHub-hosted POWER runners do not exist and a self-hosted one is the live-hardware
  track's concern (ADR-0355), not this CI job's. Not adopted now; the door stays open.
- **Scheduled-only ppc64le build.** Build amd64 on every release and ppc64le on a cron,
  publishing ppc64le less often. Rejected: it decouples the arches of a single version, so a
  tag's manifest could lack ppc64le or carry a stale one — the opposite of a reproducible
  multi-arch release.
- **Drop ppc64le from the manifest.** Rejected outright — it is the deliverable.

## Consequences

- A POWER operator can `docker pull ghcr.io/randomparity/kdive` and run the app tier;
  `docker manifest inspect` shows both arches. ADR-0356's `kdive` `build-local` row is now
  buildx-proven as promised.
- The amd64 image is unchanged: every ppc64le-specific step is behind a `TARGETARCH` guard or
  a pruned provider stage, and the amd64 `/uv` bytes and package set are identical to before.
- The release job is slower on the ppc64le leg (emulation). This is bounded to release/main
  pushes; PR CI is unaffected and stays amd64-only.
- New obligations / follow-ups:
  - **ppc64le runtime `/readyz` smoke** is unblocked only once the OIDC ppc64le mirror (#1183)
    lands; wire it then. Until then the ppc64le gate is the build-proof.
  - A lockfile change that drops the `pydantic-core` ppc64le wheel (or adds a new Rust-built
    dep with no ppc64le wheel) would reintroduce a from-source Rust build; the release job
    would fail to build ppc64le and force a revisit. This ADR's "no Rust" rests on the current
    pin, stated so the assumption is visible.
  - `uv` on ppc64le is pinned by version (`uv==<pin>`) from PyPI rather than by image digest;
    keep it in lockstep with the amd64 image pin on every bump.

## Alternatives considered

- **Add a Rust toolchain to the builder (the issue's literal ask).** Rejected: the lockfile
  inspection shows `pydantic-core` ships a `cp314` ppc64le wheel, so nothing Rust-built
  compiles from source. Adding `rustup` + a toolchain would bloat the ppc64le builder with an
  unused dependency and record a false rationale. The real from-source deps are C/C++; the
  builder is hardened for those instead.
- **Build ppc64le on every PR (add it to `ci.yml`).** Rejected: the emulated `grpcio`/`drgn`
  compile would add minutes to every PR for an arch that changes rarely. The release-path build
  plus the amd64 PR smoke catch regressions without taxing every PR; a broken ppc64le build
  surfaces on the `main` push, which is the accepted tradeoff boundary.
- **Install `uv` from PyPI on all arches (drop `COPY --from` everywhere).** Simpler, but it
  changes the amd64 image's `uv` provenance and would need amd64 re-verification for no benefit.
  The per-arch provider keeps amd64/arm64 byte-identical and confines the change to ppc64le.
- **Fetch the `uv` ppc64le release tarball by URL + sha256 instead of the PyPI wheel.** More in
  keeping with the repo's digest-pinning, but hardcodes a release URL and checksum to maintain
  in lockstep; the version-pinned PyPI wheel is uv's supported, simpler install and is still a
  prebuilt binary. Recorded as the reason the pin is a version, not a digest.
- **Build `drgn` on ppc64le without `libkdumpfile` (let its auto-detect skip it).** The build
  and `drgn --version` still pass, so CI would stay green — but ADR-0355 recorded that this
  silently breaks kdump capture on POWER, the app's core function. Rejected: `libkdumpfile-dev`
  is treated as a hard build dep, not an optional feature.
- **Assert the ppc64le image's `/readyz` in CI too.** Impossible today: `/readyz` needs the
  OIDC mock, which has no ppc64le image (ADR-0356). Deferred to the mirror (#1183) rather than
  faked; the ppc64le gate is the build-proof until then.
