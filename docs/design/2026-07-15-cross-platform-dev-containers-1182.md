# Cross-platform dev containers: ADR + arch-support matrix (#1182)

> Part of epic #1189 (cross-platform dev tooling). This is the documentation-and-guard
> sub-issue: it records the container arch strategy as an ADR, produces an authoritative
> arch-support matrix for every image the dev stack runs, and adds a CI guard that fences
> the matrix against drift. It fixes no image itself — the OIDC mirror image and the buildx
> CI job are later epic sub-issues that implement against this contract.

## Problem

`docker compose up` fails on ppc64le because at least one backing image publishes no
ppc64le manifest, but the epic has been implementing against inherited assumptions about
which images are portable. There is no written contract stating, per image, which arches it
publishes and how kdive handles a gap. Without one, a future compose change (a new backing
service, a tag bump that drops an arch) can silently re-break the ppc64le dev loop, and the
downstream epic sub-issues (the OIDC mirror, the buildx verification job) have no agreed
scope to build against.

## Empirical findings (probed 2026-07-15)

Every compose image was inspected with `docker buildx imagetools inspect <ref>` at the tag
pinned in `docker-compose.yml`. Published arches for the pinned tags:

| image (pinned ref) | compose role | amd64 | arm64 | ppc64le |
|---|---|:---:|:---:|:---:|
| `postgres:17` | core backend | ✅ | ✅ | ✅ |
| `minio/minio:RELEASE.2025-04-22T22-12-26Z` | core backend | ✅ | ✅ | ✅ |
| `minio/mc:RELEASE.2025-04-16T18-13-26Z` | core (bucket init one-shot) | ✅ | ✅ | ✅ |
| `ghcr.io/navikt/mock-oauth2-server:3.0.3` | core backend (OIDC mock) | ✅ | ✅ | ❌ |
| `prom/prometheus:v3.12.0` | observability (`obs` profile) | ✅ | ✅ | ✅ |
| `grafana/grafana:13.0.3` | observability (`obs` profile) | ✅ | ✅ | ❌ |
| `kdive:dev` (repo `Dockerfile`) | app image (built locally) | ✅ | — | base supports ppc64le |

Two findings correct the issue's stated assumptions:

1. **`minio/mc` is multi-arch at its pin** (the issue listed it only as an audit item):
   ppc64le is published. No handling needed.
2. **Grafana publishes no ppc64le image at any tag** (`13.0.3`, `latest`, and `11.6.0` all
   list only `amd64` / `arm64` / `arm/v7`). The issue assumed Grafana was multi-arch
   ("✅ confirm tag"). It is not. This is a second ppc64le gap beyond the OIDC mock.

The `kdive` image is built locally from `python:3.14.6-slim-bookworm`, whose base index
publishes ppc64le, so the multi-arch build path is available; proving the buildx build is a
later epic sub-issue, not this one.

### Why the Grafana gap is bounded

`prometheus` and `grafana` sit behind `profiles: ["obs"]` in `docker-compose.yml` — an
opt-in observability tier, not part of the default `docker compose up`. So the Grafana gap
degrades only the optional metrics dashboard on ppc64le; the core dev loop (postgres, minio,
minio/mc, oidc, kdive) is unaffected once the OIDC mock is mirrored. Of the default-profile
images, `oidc` is the only ppc64le gap — which matches the epic's "OIDC is the only
non-portable image" for the core path.

## Decisions

The container strategy the rest of the epic implements against:

1. **Mirror the OIDC mock as a multi-arch image** by repackaging the upstream standalone jar
   onto a multi-arch JRE base — no Kotlin rebuild. The upstream image's HTTP contract (token
   / JWKS / discovery endpoints) is unchanged; only the container base gains ppc64le. The
   mirror image itself is a later epic sub-issue; this ADR records that this is the chosen
   handling, so `oidc`'s matrix row reads `mirror`.
2. **Verification posture is multi-arch image builds in CI (buildx), not a POWER
   test-runner.** The epic proves the images *build* for ppc64le under `docker buildx`; it
   does not stand up a POWER CI runner to execute them. Runtime validation on real POWER is
   the separate live-hardware track (ADR-0355).
3. **Backends and the observability tier rely on upstream multi-arch images, fenced by a CI
   guard.** postgres, minio, minio/mc, and prometheus are used as-is because their pinned
   tags publish ppc64le. Grafana is used as-is with a documented ppc64le gap (opt-in tier).
   A CI guard keeps the compose image set and the matrix from drifting apart.

## The CI guard contract

A new stdlib-only guard, `scripts/check_container_arch_matrix.py`, wired into `just ci` as
its own recipe (`container-arch-check`) so CI gates it individually (per this repo's
per-recipe CI invocation).

**Source of truth.** The arch-support matrix lives in ADR-0356 inside an HTML-comment-marked
block so the guard can locate it unambiguously:

```
<!-- arch-matrix:begin -->
| Image | ... | Handling |
| `postgres:17` | ... | rely-on-upstream |
...
<!-- arch-matrix:end -->
```

The first column of each data row is the image reference in backticks; the last column is
the handling token.

**What the guard checks (static, no docker/network/POWER):**

1. **Set equality.** The set of `image:` references in `docker-compose.yml` (de-duplicated;
   `kdive:dev` appears four times → one entry) equals the set of image refs in the matrix
   block. It reports, by name, any image present in compose but missing from the matrix
   (the regression case: a new/changed backing image slipped in), and any matrix row with no
   corresponding compose image (a stale row).
2. **Handling validity.** Every matrix row's handling token is one of a fixed set:
   `rely-on-upstream`, `mirror`, `build-local`. An unknown token fails, so a row cannot claim
   an undefined handling.

**What the guard deliberately does not do:** it does not run `docker manifest inspect` or
otherwise probe live registries. Live arch verification needs docker + network and cannot run
in the standard offline CI test job; the buildx multi-arch *build* (epic sub-issue) is the
live check, and re-confirming published arches on a tag bump is a human review step prompted
by the guard failing when the pinned ref changes.

**Regression behavior.** Because the matrix column carries the full `repo:tag` reference, a
compose tag bump (e.g. Dependabot) changes the compose ref and fails the guard until the
matrix row is updated — forcing a human to re-confirm the new tag's arch coverage. This is
the intended fence: the matrix cannot silently fall behind compose.

## Follow-ups recorded (not fixed here)

- **OIDC mirror image** — build and publish the multi-arch mirror, then repoint
  `docker-compose.yml` and `deploy/helm/kdive/values.yaml` at it. Epic #1189 sub-issue.
- **Helm demo OIDC is the same gap.** `deploy/helm/kdive/values.yaml:127` sets
  `demo.oidc.image: ghcr.io/navikt/mock-oauth2-server:3.0.3`, consumed by
  `deploy/helm/kdive/templates/demo/oidc.yaml`. The k8s demo path inherits the identical
  ppc64le gap and must be repointed at the mirror when it exists. Recorded as a follow-up so
  the demo is not silently left amd64-only.
- **Grafana ppc64le gap.** No upstream ppc64le image exists; the `obs` profile's dashboard is
  unavailable on ppc64le. Recorded as a follow-up (options: a ppc64le-capable dashboard
  alternative, or accepting the opt-in tier's gap). Prometheus (the metrics store) is
  unaffected.
- **Keycloak (production-OIDC track, #349/#350/#351) also lacks ppc64le**
  (`quay.io/keycloak/keycloak:26.4`). That is a separate track's POWER gap, noted for
  cross-reference only.

## Acceptance criteria

1. `docs/adr/0356-cross-platform-dev-containers.md` exists, carrying the matrix in a
   `<!-- arch-matrix:begin -->`/`<!-- arch-matrix:end -->` block, and records decisions 1–3
   and the follow-ups. It opens **Proposed** and is flipped to **Accepted** (both the ADR
   `Status` line and its `docs/adr/README.md` index row) in the merging PR.
2. `docs/adr/README.md` has an index row for 0356.
3. `scripts/check_container_arch_matrix.py` exists, is stdlib-only, and:
   - passes against the current compose file + matrix;
   - fails (non-zero, with a named diff) when an image is added to / removed from compose
     without a matching matrix change;
   - fails when a matrix row carries an unknown handling token.
4. `just container-arch-check` is a recipe and is a member of the aggregate `just ci` recipe.
5. The guard has unit tests covering: the happy path, an image-missing-from-matrix drift, a
   matrix-row-missing-from-compose drift, and an invalid handling token.
6. Green: `just adr-status-check`, `just docs-links`, `just docs-paths`, `just check-mermaid`,
   `just container-arch-check`, and the full `just ci`.

## Risks and mitigations

- **Matrix parser brittleness.** A malformed matrix block (missing marker, missing column)
  must fail loudly, not pass vacuously. Mitigation: the guard errors if the marked block is
  absent or a row has too few columns, and a unit test asserts a vacuous/empty matrix fails.
- **Compose image extraction misses a service.** Regex-based `image:` extraction could miss
  an unusual line shape. Mitigation: a unit test pins the current full compose image set so a
  parser change that drops a service is caught; the extractor targets the well-formed
  `    image: <ref>` line shape compose uses throughout.
- **ADR status timing.** Keeping the ADR **Proposed** while iterating is safe: the
  `adr-status-check` shipped-but-Proposed rule only fires on a `src/` citation, and this
  change cites the ADR only from `scripts/` and docs. The Proposed→Accepted flip happens in
  the ship commit, flipping the ADR file and the index row together so the guard stays green.

## Non-goals

- Building or publishing the OIDC mirror image (epic sub-issue).
- Standing up a buildx multi-arch CI job (epic sub-issue).
- Any live `docker manifest inspect` / registry probe in CI.
- Fixing the Grafana or Keycloak ppc64le gaps.
