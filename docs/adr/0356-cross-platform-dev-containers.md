# ADR 0356 — Cross-platform dev containers: arch-support matrix and drift guard

- **Status:** Accepted
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
matrix here, and add a CI guard (`yaml.safe_load` over compose; PyYAML is already a hard
dependency) that fences the matrix against compose drift.

1. **Mirror the OIDC mock as a multi-arch image** by repackaging the upstream standalone jar
   onto a multi-arch JRE base — no Kotlin rebuild. The upstream HTTP contract (token / JWKS /
   discovery) is unchanged; only the container base gains ppc64le. The mirror image is a later
   epic sub-issue; this ADR fixes `oidc`'s handling as `mirror`.
2. **Verification posture is multi-arch image builds in CI (buildx), not a POWER
   test-runner.** The epic proves the images *build* for ppc64le under `docker buildx`; it does
   not stand up a POWER CI runner. Runtime validation on real POWER is the separate
   live-hardware track (ADR-0355).
3. **Backends and the observability tier rely on upstream multi-arch images, fenced by a CI
   guard that makes the core-loop invariant machine-checkable.** Postgres, MinIO, `mc`, and
   Prometheus are used as-is (their pinned tags publish ppc64le); Grafana is used as-is with a
   knowingly-accepted ppc64le gap in the opt-in tier. The guard
   `scripts/check_container_arch_matrix.py` (recipe `just container-arch-check`, a member of
   `just ci`) statically asserts, with no live registry probe: (a) the compose image set
   equals the matrix image set; (b) every row's handling is a known token; (c) a
   `rely-on-upstream` row's ppc64le cell is exactly ✅; (d) an `accept-gap` row's image is used
   only by opt-in (profiled) compose services; (e) a `mirror` row cites a tracking issue; and
   (f) a `build-local` row's image is built by a compose service (`build:`). Every token thus
   carries a checked ppc64le obligation — none is an unconstrained escape hatch.

### Arch-support matrix

Published architectures per image (probed 2026-07-15) and kdive's handling. Arch columns use a
fixed alphabet — `✅` published, `❌` not published, `—` not applicable (a `build-local` image
is built, not pulled per-arch; arch notes live in the `Role` column). Handling tokens:

| token | meaning | guard obligation |
|---|---|---|
| `rely-on-upstream` | use the upstream image as-is | ppc64le cell = ✅ |
| `mirror` | upstream lacks ppc64le; kdive repackages it | row cites a tracking issue `#NNNN` |
| `publish-mirror` | kdive builds + publishes a multi-arch mirror (#1184, ADR-0358) | ppc64le cell = ✅ and compose pins it by an `@sha256:` digest |
| `build-local` | built from the repo `Dockerfile` | a compose service that uses it has `build:` |
| `accept-gap` | knowingly unsupported on ppc64le | image used only by opt-in (profiled) services |

<!-- arch-matrix:begin -->
| Image | Role | amd64 | arm64 | ppc64le | Handling |
|---|---|:---:|:---:|:---:|---|
| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
| `minio/minio:RELEASE.2025-04-22T22-12-26Z` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
| `minio/mc:RELEASE.2025-04-16T18-13-26Z` | core (bucket-init one-shot) | ✅ | ✅ | ✅ | rely-on-upstream |
| `ghcr.io/randomparity/mock-oauth2-server@sha256:bdf70ffa80b3aec360917da5a2bbafd1cd8d93c278e5b00e79d4a85251a9fff6` | core backend (OIDC mock; kdive-published multi-arch mirror of the upstream jar, #1183/#1184 · ADR-0357/0358; compose keeps `build: ./deploy/mock-oidc` as a local fallback) | ✅ | ❌ | ✅ | publish-mirror |
| `prom/prometheus:v3.12.0` | observability (`obs` profile) | ✅ | ✅ | ✅ | rely-on-upstream |
| `grafana/grafana:13.0.3` | observability (`obs` profile) | ✅ | ✅ | ❌ | accept-gap |
| `kdive:dev` | app image (repo Dockerfile; base publishes ppc64le, buildx-proven in #1185) | — | — | — | build-local |
<!-- arch-matrix:end -->

Notes on the two gap rows:

- **OIDC mock** — the ppc64le gap is now closed. The upstream `ghcr.io/navikt/mock-oauth2-server:3.0.3`
  (amd64/arm64 only) was first repackaged as an in-repo `build-local` image (#1183, ADR-0357),
  then built and published by kdive as a `linux/amd64,linux/ppc64le` GHCR manifest that compose
  pins by digest (#1184, ADR-0358) — handling `publish-mirror`. The row is fail-closed like
  `rely-on-upstream` (ppc64le ✅) and additionally requires the compose `@sha256:` digest pin;
  `build: ./deploy/mock-oidc` is retained as the documented local fallback.
- **`grafana/grafana:13.0.3`** — handled `accept-gap`: no upstream ppc64le image exists and it
  is opt-in (`obs` profile), so the ppc64le dashboard is unavailable pending a follow-up. The
  guard permits `accept-gap` only because no un-profiled service uses grafana; the same shape
  on a core-loop image would fail. Prometheus (the metrics store) is unaffected.

The `kdive` row is `build-local`: it is built from `python:3.14.6-slim-bookworm`, whose base
index publishes ppc64le, so the buildx multi-arch build path is available (proving that build
in CI is a later epic sub-issue, #1185). Its arch cells are `—` because a locally-built image
has no pulled per-arch manifest; the guard verifies instead that a compose service builds it.

## Consequences

- The epic's downstream sub-issues (OIDC mirror, buildx job) build against a written, verified
  contract instead of ad-hoc decisions.
- A compose change that adds, removes, or retags a backing image fails `container-arch-check`
  until the matrix row is updated. A retag *prompts* a human re-probe of the new reference's
  arch coverage; it does not by itself verify one. The enforcement teeth are in assertion (c):
  a `rely-on-upstream` row recorded (or corrected) to ppc64le ❌ fails, forcing a `mirror` /
  `build-local` / `accept-gap` handling or a real fix. A default-profile image can no longer
  sit silently broken behind a `rely-on-upstream` label, and an `accept-gap` cannot be applied
  to a core-loop image.
- The guard is static, so two residuals remain, both stated rather than left implicit:
  - It does **not** detect an *upstream* arch regression (a tag that stops publishing ppc64le
    at an unchanged pin) — a recorded ✅ would go stale invisibly. Covered by the buildx build
    job (which fails to build the missing arch) and by re-probing on the tag bumps the guard
    forces.
  - It cannot force a `mirror` to *complete*. A default-profile `mirror` gap is required to
    cite a tracking issue (assertion e), so it is a visible, tracked follow-up rather than a
    silent green-forever state — but the guard checks that the issue is *named*, not that it is
    *closed*. Closing it is the follow-up sub-issue's job (#1183/#1184), not the guard's.
  The guard is the drift-and-labelling fence, not a registry probe or a project tracker.
- Follow-ups this decision creates:
  - ~~Build the multi-arch OIDC mirror; repoint `docker-compose.yml` at it.~~ Done in #1183
    (ADR-0357) + #1184 (ADR-0358): `deploy/mock-oidc` builds the upstream jar on a multi-arch
    JRE, the `publish-mock-oidc` workflow publishes it as an amd64/ppc64le GHCR manifest, and
    the compose `oidc` service pins that manifest by digest (`publish-mirror`) with a local
    `build:` fallback. The Helm `values.yaml` repoint stays open below — a k8s deploy pulls,
    so it can now consume this same published mirror.
  - Repoint the Helm demo OIDC (`values.yaml:127` → `templates/demo/oidc.yaml`) at the mirror;
    it inherits the identical ppc64le gap and would otherwise stay amd64-only on k8s.
  - Fence the Helm `values.yaml` backing-image set (it pins the same images independently of
    compose and is out of this guard's compose-only scope), beyond the OIDC repoint above.
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
- **Guard checks only row existence + a valid handling token (no arch/profile coupling).**
  This was the first cut; it fails the very regression the guard exists to prevent — a new
  default-profile image with a `❌ rely-on-upstream` row passes green while the core ppc64le
  loop is broken. Coupling the ppc64le cell and compose profile into assertions (c)/(d) makes
  the one invariant that matters machine-checkable instead of a prose promise.
- **Gate only `rely-on-upstream`/`accept-gap`, leave `mirror`/`build-local` unchecked.** That
  leaves an escape hatch: a broken core-loop image relabelled `mirror` (or a pulled image
  mislabelled `build-local`) would pass green forever. Assertions (e)/(f) bound both — a
  `mirror` must name its tracking issue, a `build-local` must actually be built by a compose
  service — so no token is an unconstrained bypass. A static guard still cannot force a
  `mirror` to *complete*; that residual is stated in Consequences.
- **Encode the Grafana gap as `rely-on-upstream` with a prose "it's opt-in" note.** Then a
  broken default-profile image is byte-identical to an accepted opt-in gap, to both the guard
  and a human scanner. A distinct `accept-gap` token, permitted only behind a profile, makes
  the opt-in reasoning enforced rather than commentary.
- **Fence the Helm `values.yaml` image set in the same guard now.** The Helm pins are
  independently versioned from compose (a k8s deploy is a different target); folding them into
  one matrix would couple two files that may legitimately diverge. Left as a recorded
  follow-up with its own fence rather than force-coupled here.
- **Emulate the upstream OIDC container on ppc64le via qemu-user instead of mirroring.** The
  JVM deadlocks / segfaults under qemu-user emulation (recorded in ADR-0355); a native
  multi-arch mirror is the working path.
- **Rebuild the OIDC issuer from Kotlin source for ppc64le.** Unnecessary — the upstream jar
  is arch-neutral bytecode; repackaging it onto a multi-arch JRE base avoids a source build.
