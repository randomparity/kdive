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
   tags publish ppc64le. Grafana is used as-is with a knowingly-accepted ppc64le gap in the
   opt-in tier. A CI guard keeps the compose image set and the matrix from drifting apart
   **and** makes the ppc64le-core-loop invariant machine-checkable (see below).

## The CI guard contract

A new stdlib-only guard, `scripts/check_container_arch_matrix.py`, wired into `just ci` as
its own recipe (`container-arch-check`) so CI gates it individually (per this repo's
per-recipe CI invocation).

**Source of truth.** The arch-support matrix lives in ADR-0356 inside an HTML-comment-marked
block so the guard can locate it unambiguously:

```
<!-- arch-matrix:begin -->
| Image | Role | amd64 | arm64 | ppc64le | Handling |
|---|---|:---:|:---:|:---:|---|
| `postgres:17` | core backend | ✅ | ✅ | ✅ | rely-on-upstream |
...
<!-- arch-matrix:end -->
```

**Row/column parsing (specified so a header row cannot cause an always-fail or vacuous
pass).** Inside the marked block the guard reads the table's **header row** to locate the
`ppc64le` and `Handling` columns by name (the first column is the Image column). A **data
row** is one whose first cell is a backtick-wrapped token (`` `repo:tag` ``); the header row
(first cell `Image`) and the `|---|` separator row (first cell `---`) are not data rows and
are skipped. A block with no data rows, a missing marker, a missing `ppc64le`/`Handling`
header, or a data row with too few cells is a hard error (fail loudly, never pass vacuously).

**Handling tokens.** Each data row's handling is one of a fixed set; every token carries a
ppc64le obligation the guard checks, so no token is an unconstrained escape hatch:

| token | meaning | guard obligation |
|---|---|---|
| `rely-on-upstream` | use the upstream image as-is | ppc64le cell = ✅ |
| `mirror` | upstream lacks ppc64le; kdive repackages it | row cites a tracking issue `#NNNN` |
| `build-local` | built from the repo `Dockerfile` | a compose service that uses it has a `build:` key |
| `accept-gap` | knowingly unsupported on ppc64le | image is used only by opt-in (profiled) services |

**Arch-column alphabet.** The three arch columns (`amd64`, `arm64`, `ppc64le`) use a fixed
alphabet: `✅` (published), `❌` (not published), `—` (not applicable — e.g. a `build-local`
image is built, not pulled per-arch). Any other value in an arch column is a hard error, so
free prose cannot hide in the column assertion 3 reads (arch notes go in the `Role` column).

**Compose parsing (stdlib, no pyyaml).** The guard does an indentation-aware pass that enters
service-scanning only inside the top-level `services:` mapping and terminates at the next
column-0 key — so the top-level `x-*` anchor maps (before `services:`) and `volumes:` (after)
are never read as services. Inside `services:`, each 2-space-indented `<name>:` starts a
service; within it `    image: <ref>` gives the image, a `    profiles:` key marks the service
opt-in, and a `    build:` key marks it locally built. An image is **default-profile** if at
least one service using it has no `profiles:` key (`docker compose up` pulls it); it is
**built** if at least one service using it has a `build:` key. `kdive:dev` appears on four
default services (one has `build: .`) → one default-profile, built entry.

**What the guard asserts (static, no docker/network/POWER):**

1. **Set equality.** The de-duplicated set of compose `image:` references equals the set of
   matrix image refs. Reports, by name, any image in compose but missing from the matrix (the
   regression case: a new/changed backing image slipped in) and any matrix row with no
   compose image (a stale row).
2. **Handling validity.** Every row's handling token is in the fixed set above.
3. **`rely-on-upstream` ⟹ ppc64le published.** The row's ppc64le cell is exactly `✅` (after
   trim); any other value on such a row is a hard error (fail-closed, not a "contains" match).
   This is the core fence: an image relied on as-is that does not (or no longer does) publish
   ppc64le fails, forcing a `mirror`/`build-local`/`accept-gap` handling or a real fix — it
   cannot sit silently broken with a `rely-on-upstream` label.
4. **`accept-gap` ⟹ opt-in only.** The image must not be default-profile (no un-profiled
   service uses it). A knowingly-accepted gap cannot mask a broken *core-loop* image; moving
   such an image onto a default service fails the guard.
5. **`mirror` ⟹ tracked.** The row must cite a tracking issue (`#NNNN`) so a default-profile
   gap under a `mirror` label is a visible, tracked follow-up — not a silent, green-forever
   bypass of assertion 3 (relabelling a broken `rely-on-upstream` image to `mirror` now
   requires naming the issue that will fix it).
6. **`build-local` ⟹ actually built.** At least one compose service using the image has a
   `build:` key. `build-local` is exempt from the ppc64le-cell gate because its arch is proven
   by the buildx build job, not a pulled manifest; assertion 6 stops the token being borrowed
   by a pulled upstream image to dodge assertion 3.

Under these rules the current stack resolves as: postgres/minio/mc/prometheus =
`rely-on-upstream` (ppc64le ✅); `oidc` = `mirror` (default-profile gap, tracked #1183);
grafana = `accept-gap` (opt-in `obs`); `kdive:dev` = `build-local` (has `build: .`).

**What the guard deliberately does not do:** it does not run `docker manifest inspect` or
otherwise probe live registries. Live arch verification needs docker + network and cannot run
in the standard offline CI test job; the buildx multi-arch *build* (epic sub-issue) is the
live arch check.

**Regression behavior (stated to what is actually enforced).** Because the matrix column
carries the full `repo:tag` reference, a compose tag bump (e.g. Dependabot) changes the
compose ref and fails set-equality until the matrix row is edited to match — which *prompts*,
but does not by itself *verify*, a human re-probe of the new tag's arches. The teeth are in
assertion 3: if the human records the new tag as ppc64le ❌ (or a reviewer corrects a stale
✅), a `rely-on-upstream` row fails and forces a handling decision. Static parsing cannot
detect an upstream arch drop at an unchanged pin; the buildx build job covers that.

## Follow-ups recorded (not fixed here)

- **OIDC mirror image** — build and publish the multi-arch mirror, then repoint
  `docker-compose.yml` and `deploy/helm/kdive/values.yaml` at it. Epic #1189 sub-issue.
- **Helm demo OIDC is the same gap.** `deploy/helm/kdive/values.yaml:127` sets
  `demo.oidc.image: ghcr.io/navikt/mock-oauth2-server:3.0.3`, consumed by
  `deploy/helm/kdive/templates/demo/oidc.yaml`. The k8s demo path inherits the identical
  ppc64le gap and must be repointed at the mirror when it exists. Recorded as a follow-up so
  the demo is not silently left amd64-only.
- **Helm `values.yaml` image set is not fenced by this guard.** `deploy/helm/kdive/values.yaml`
  pins the same backing images (postgres, minio, minio/mc, oidc, prometheus) for the k8s
  deploy path, outside the guard's compose-only scope. Those pins can legitimately differ from
  compose (a k8s deploy is a different target), so folding them into the same matrix now would
  couple two independently-versioned files. Recorded as a distinct follow-up: fence the Helm
  image set (against its own matrix, or by asserting parity with compose) beyond the OIDC
  repoint below. See Non-goals.
- **Grafana ppc64le gap.** No upstream ppc64le image exists; the `obs` profile's dashboard is
  unavailable on ppc64le. Encoded as `accept-gap` (opt-in only) so the guard permits it while
  forbidding the same shape on a core-loop image. Follow-up options: a ppc64le-capable
  dashboard alternative, or continuing to accept the opt-in tier's gap. Prometheus (the
  metrics store) is unaffected.
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
   - fails when a matrix row carries an unknown handling token;
   - fails when a `rely-on-upstream` row's ppc64le cell is not exactly ✅ (including empty or
     prose);
   - fails when any arch column holds a value outside `{✅, ❌, —}`;
   - fails when an `accept-gap` row's image is used by a default-profile (un-profiled) service;
   - fails when a `mirror` row cites no tracking issue (`#NNNN`);
   - fails when a `build-local` row's image is not built by any compose service (`build:`);
   - fails loudly (not vacuously) when the matrix block is missing, empty, or malformed.
4. `just container-arch-check` is a recipe and is a member of the aggregate `just ci` recipe.
5. The guard has unit tests covering: the happy path; image-missing-from-matrix drift;
   matrix-row-missing-from-compose drift; an invalid handling token; a `rely-on-upstream` row
   with ppc64le ❌; a `rely-on-upstream` row with a malformed/empty/prose ppc64le cell; an
   out-of-alphabet arch cell; an `accept-gap` row on a default-profile image; a `mirror` row
   with no `#NNNN`; a `build-local` row whose image no service builds; a malformed/empty matrix
   block (header/separator rows are not mistaken for data rows); and a fixture with a top-level
   `volumes:` block proving its 2-space-indented children are not parsed as services.
6. Green: `just adr-status-check`, `just docs-links`, `just docs-paths`, `just check-mermaid`,
   `just container-arch-check`, and the full `just ci`.

## Risks and mitigations

- **Matrix parser brittleness.** A malformed matrix block (missing marker, missing column)
  must fail loudly, not pass vacuously. Mitigation: the guard errors if the marked block is
  absent or a row has too few columns, and a unit test asserts a vacuous/empty matrix fails.
- **Compose image extraction misses a service or over-reads a non-service block.** The
  indentation-aware scan could miss a line shape or ingest a top-level `volumes:`/`x-*` child
  as a service. Mitigation: the scan is bounded to inside the top-level `services:` mapping
  (stops at the next column-0 key); a unit test pins the current full compose image set with
  each service's default/opt-in and built/pulled classification, and a fixture with a
  top-level `volumes:` block asserts its children are not counted as services.
- **A handling token used as an escape hatch.** `mirror` and `build-local` are exempt from the
  ppc64le-cell gate; without bounds they could be borrowed to dodge assertion 3. Mitigation:
  assertion 5 requires a `mirror` row to cite a tracking issue and assertion 6 requires a
  `build-local` image to actually be built by a compose service — so every token has a checked
  obligation. Unit tests pin both failing cases.
- **Static guard cannot catch an upstream arch drop at an unchanged pin.** Accepted residual
  risk: assertion 3 fences a `rely-on-upstream` row whose *recorded* ppc64le cell is not ✅,
  but a tag that silently stops publishing ppc64le without a pin change is invisible to static
  parsing. The buildx multi-arch build job (epic sub-issue) is the live arch check; the guard
  is the drift-and-labelling fence, not a registry probe. This is stated in the ADR
  consequences, not left implicit.
- **ADR status timing.** Keeping the ADR **Proposed** while iterating is safe: the
  `adr-status-check` shipped-but-Proposed rule only fires on a `src/` citation, and this
  change cites the ADR only from `scripts/` and docs. The Proposed→Accepted flip happens in
  the ship commit, flipping the ADR file and the index row together so the guard stays green.

## Non-goals

- Building or publishing the OIDC mirror image (epic sub-issue).
- Standing up a buildx multi-arch CI job (epic sub-issue).
- Any live `docker manifest inspect` / registry probe in CI.
- Fencing `deploy/helm/kdive/values.yaml` image drift — the guard is compose-only this issue;
  Helm image-set fencing is a recorded follow-up (the Helm pins are independently versioned).
- Fixing the Grafana or Keycloak ppc64le gaps.
