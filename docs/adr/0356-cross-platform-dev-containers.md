# ADR 0356 — Cross-platform dev containers: arch-support matrix and drift guard

- **Status:** Proposed
- **Date:** 2026-07-15
- **Issue:** #1182
- **Epic:** #1189 (cross-platform dev tooling)
- **Related:** ADR-0355 (#1156 native POWER validation — records that the mock-OIDC issuer
  runs as native ppc64le JVM bytecode because no ppc64le container image exists)

## Context

The developer stack is a `docker-compose.yml` of backing services (Postgres, MinIO + its
`mc` init one-shot, a mock OIDC issuer), an opt-in observability tier (Prometheus, Grafana),
and the locally-built `kdive` application image. Epic #1189 brings this stack up on ppc64le
in addition to amd64/arm64.

`docker compose up` fails on ppc64le because at least one backing image publishes no ppc64le
manifest, and the epic has been reasoning from inherited assumptions about which images are
portable. There was no written, verified contract stating — per image — which architectures
it publishes and how kdive handles a gap. Downstream sub-issues (an OIDC mirror image, a
buildx verification job) need that contract to build against, and a future compose change (a
new backing service, a tag bump that drops an architecture) can silently re-break the ppc64le
loop with nothing to catch it.

Each compose image was inspected with `docker buildx imagetools inspect <ref>` at its pinned
tag on 2026-07-15. Two results correct prior assumptions: `minio/mc` **is** multi-arch at its
pin (ppc64le published), and Grafana publishes **no** ppc64le image at any tag (`13.0.3`,
`latest`, `11.6.0` all list only amd64 / arm64 / arm/v7) — a second ppc64le gap beyond the
OIDC mock. Prometheus and Grafana sit behind `profiles: ["obs"]`, so the Grafana gap degrades
only the opt-in observability tier, not the core `docker compose up` loop; of the
default-profile images, the OIDC mock is the only ppc64le gap.

## Decision

We will record the container arch strategy in this ADR, carry the authoritative arch-support
matrix here, and add a stdlib CI guard that fences the matrix against compose drift.

1. **Mirror the OIDC mock as a multi-arch image** by repackaging the upstream standalone jar
   onto a multi-arch JRE base — no Kotlin rebuild. The upstream HTTP contract (token / JWKS /
   discovery) is unchanged; only the container base gains ppc64le. The mirror image is a later
   epic sub-issue; this ADR fixes `oidc`'s handling as `mirror`.
2. **Verification posture is multi-arch image builds in CI (buildx), not a POWER
   test-runner.** The epic proves the images *build* for ppc64le under `docker buildx`; it does
   not stand up a POWER CI runner. Runtime validation on real POWER is the separate
   live-hardware track (ADR-0355).
3. **Backends and the observability tier rely on upstream multi-arch images, fenced by a CI
   guard.** Postgres, MinIO, `mc`, and Prometheus are used as-is (their pinned tags publish
   ppc64le); Grafana is used as-is with a documented ppc64le gap in the opt-in tier. The guard
   `scripts/check_container_arch_matrix.py` (recipe `just container-arch-check`, a member of
   `just ci`) asserts, statically, that the compose image set equals the matrix image set and
   that every row's handling is a known token — no live registry probe.

### Arch-support matrix

Published architectures per image (probed 2026-07-15) and kdive's handling. Handling tokens:
`rely-on-upstream` (use the upstream image), `mirror` (repackage as a kdive multi-arch image),
`build-local` (built from the repo `Dockerfile`).

<!-- arch-matrix:begin -->
| Image | Role | amd64 | arm64 | ppc64le | Handling |
|---|---|:---:|:---:|:---:|---|
| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
| `minio/minio:RELEASE.2025-04-22T22-12-26Z` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
| `minio/mc:RELEASE.2025-04-16T18-13-26Z` | core (bucket-init one-shot) | ✅ | ✅ | ✅ | rely-on-upstream |
| `ghcr.io/navikt/mock-oauth2-server:3.0.3` | core backend (OIDC mock) | ✅ | ✅ | ❌ | mirror |
| `prom/prometheus:v3.12.0` | observability (`obs` profile) | ✅ | ✅ | ✅ | rely-on-upstream |
| `grafana/grafana:13.0.3` | observability (`obs` profile) | ✅ | ✅ | ❌ | rely-on-upstream |
| `kdive:dev` | app image (repo Dockerfile) | ✅ | — | base publishes ppc64le | build-local |
<!-- arch-matrix:end -->

Notes on the two gap rows:

- **`ghcr.io/navikt/mock-oauth2-server:3.0.3`** — handled by `mirror` (decision 1). It is a
  default-profile image, so its gap blocks the core ppc64le loop until the mirror lands.
- **`grafana/grafana:13.0.3`** — handled `rely-on-upstream` with a known ppc64le gap. It is
  opt-in (`obs` profile) and no upstream ppc64le image exists, so the ppc64le dashboard is
  unavailable pending a follow-up. Prometheus (the metrics store) is unaffected.

The `kdive` row is `build-local`: it is built from `python:3.14.6-slim-bookworm`, whose base
index publishes ppc64le, so the buildx multi-arch build path is available. Proving that build
in CI is a later epic sub-issue.

## Consequences

- The epic's downstream sub-issues (OIDC mirror, buildx job) build against a written, verified
  contract instead of ad-hoc decisions.
- A compose change that adds, removes, or retags a backing image fails `container-arch-check`
  until the matrix row is updated, forcing a human to re-confirm the new reference's arch
  coverage. The matrix cannot silently fall behind compose.
- The guard is static: it does not detect an *upstream* arch regression (a tag that stops
  publishing ppc64le at an unchanged pin). That risk is covered by the buildx build job (which
  fails to build the missing arch) and by re-probing on the deliberate tag bumps the guard
  forces.
- Follow-ups this decision creates:
  - Build and publish the multi-arch OIDC mirror; repoint `docker-compose.yml` and
    `deploy/helm/kdive/values.yaml` at it.
  - Repoint the Helm demo OIDC (`values.yaml:127` → `templates/demo/oidc.yaml`) at the mirror;
    it inherits the identical ppc64le gap and would otherwise stay amd64-only on k8s.
  - Resolve or accept the Grafana ppc64le gap for the opt-in `obs` tier.
  - Keycloak (production-OIDC track #349/#350/#351) also lacks a ppc64le manifest
    (`quay.io/keycloak/keycloak:26.4`); that is a separate track's POWER gap, cross-referenced
    only.

## Alternatives considered

- **Put the matrix in `docs/development/` instead of the ADR.** Splits the decision from its
  data and gives the guard a second file to track. Keeping the matrix in the ADR, in a marked
  block, makes the ADR the single source both humans and the guard read.
- **Have the guard run `docker manifest inspect` to verify arches live.** Needs docker +
  network, cannot run in the offline CI test job, and is flaky against registry availability.
  The static compose↔matrix check plus the buildx build job cover regression without a live
  probe.
- **Match matrix rows on the image name without the tag.** A tag bump would then pass the
  guard silently, defeating the re-confirmation the fence exists to force. Matching the full
  `repo:tag` reference makes every bump a prompted review.
- **Emulate the upstream OIDC container on ppc64le via qemu-user instead of mirroring.** The
  JVM deadlocks / segfaults under qemu-user emulation (recorded in ADR-0355); a native
  multi-arch mirror is the working path.
- **Rebuild the OIDC issuer from Kotlin source for ppc64le.** Unnecessary — the upstream jar
  is arch-neutral bytecode; repackaging it onto a multi-arch JRE base avoids a source build.
