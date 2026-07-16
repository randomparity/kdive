# ADR 0365 — The Helm chart `version` tracks chart packaging, independent of `appVersion`

- **Status:** Accepted
- **Date:** 2026-07-15
- **Composes with:** [ADR-0041](0041-versioning-release-process.md) (versioning policy &
  release process — decision 4 names `pyproject` `[project].version` as the single source of
  truth for the *application* version; this ADR records how the packaged Helm chart's own
  version relates to it)
- **Issue:** #1210 · Epic: #1199 (v0.3.0 release-readiness, Workstream E)

## Context

Preparing the v0.3.0 cut surfaced an apparent version drift: `pyproject` `[project].version`
is `0.3.0` while `deploy/helm/kdive/Chart.yaml` carries `version: 0.4.0`. The release-readiness
spec (`docs/design/2026-07-15-release-readiness-cross-platform-design.md` §"Workstream E")
asks that the intended relationship be decided and encoded so `chart-version-check` enforces it
rather than flagging a false drift (or masking a real one).

A Helm chart has **two** version fields with distinct meanings (standard Helm convention):

- `appVersion` — the version of the *application* the chart deploys. For kdive this must mirror
  the ADR-0041 source of truth, `pyproject` `[project].version`.
- `version` — the version of the *chart package itself* (its templates, values schema, and
  packaging). It moves when the chart's own contents change, on its own SemVer track, and is the
  version Helm uses for `helm package`/repo indexing and upgrade ordering.

The two are not required to be equal, and conflating them is the actual drift risk: forcing
`version` down to `appVersion` would discard the chart's independent packaging history, and it
would break Helm's upgrade ordering the moment the chart changes without an app-version bump.
Here `appVersion: "0.3.0"` already equals `pyproject` `0.3.0` — the release target **is** v0.3.0,
and there is no application-version drift. Only `version` differs, which is expected.

## Decision

1. **`appVersion` mirrors `pyproject` `[project].version`; it is the app-version invariant.**
   This is the ADR-0041 source of truth reaching the chart. `just set-version` rewrites
   `Chart.yaml` `appVersion` alongside `pyproject`, and `chart-version-check` (a `just ci` gate)
   fails the build on any `appVersion` ≠ `pyproject` drift. The intended state for the v0.3.0 cut
   is `appVersion == pyproject == 0.3.0`, which is green today.

2. **The chart `version` field tracks chart packaging and moves independently of `appVersion`.**
   It follows its own SemVer track for changes to the chart's templates/values/packaging and is
   maintained by hand when the chart changes. `chart-version-check` deliberately does **not**
   constrain it — coupling the two is neither required by Helm nor desirable (it would erase the
   chart's packaging history and break upgrade ordering). The current `version: 0.4.0` is a valid
   chart-packaging version and is left as-is; it is not drift.

## Consequences

- `chart-version-check` stays scoped to the one invariant that matters — `appVersion == pyproject`
  — so it is green for the intended state and red on genuine application-version drift. No change
  to the recipe's logic is needed; a clarifying comment records why `version` is out of its scope.
- The v0.3.0 cut proceeds with `appVersion` already reconciled; the Workstream E dry-run is not
  blocked on a version mismatch.
- A future change to the chart's own templates/values bumps `version` without touching
  `appVersion`, and vice versa — each on its own track.

## Rejected alternatives

- **Force `Chart.yaml` `version` to `0.3.0` to "match".** Rejected — it conflates two distinct
  Helm fields, discards the chart's independent packaging history, and breaks Helm upgrade
  ordering whenever the chart changes without an app-version bump.
- **Extend `chart-version-check` to also assert `version == appVersion`.** Rejected — it would
  encode the same conflation as an enforced guard and turn every legitimate chart-only bump into a
  CI failure.
