# SeaweedFS bundled S3 backend contract — Design

## Problem

Archived MinIO images underpin KDIVE's bundled S3 paths. The replacement must preserve the
ADR-0017 behavior KDIVE consumes and satisfy ADR-0356's amd64, arm64, and ppc64le posture
without changing external S3 users or silently reusing persisted MinIO data.

## Scope

ADR-0647 selects SeaweedFS 4.46 at commit `d997fba1575583a89cf0cc50dc0150642286c86d` and
defines a KDIVE-built `weed mini` image contract. It requires local-build and optional
digest-pull paths, manifest evidence, compatibility proof, and an empty-new-volume transition.
It assigns image build/proof to #2446 and consumer migration to #2445. It does not implement
either issue, alter Python/storage APIs, or migrate data.

## Failure model

- **Actors and deployments:** local Compose operators, Helm-demo operators, CI image builders,
  and test-fixture maintainers; external S3 deployments are outside this decision.
- **Invariants and assets at stake:** versioned artifact retrieval/deletion, presigned transfers,
  architecture support, and recoverable developer data transition.
- **Accepted failure classes:** an old MinIO volume is unreadable by SeaweedFS because retaining
  it unchanged bounds rollback; an unavailable optional published image falls back to local build.
- **Covered elsewhere:** #2446 owns source build, manifest, and runtime evidence; #2445 owns
  Compose, Helm, fixture, and matrix adoption; ADR-0017 owns ObjectStore semantics.

## Architecture

The source pin is the reproducible input. A future image uses `weed mini -dir=/data` with its
bucket and credentials explicitly configured. The default Compose image is locally built, like
mock OIDC; an override selects a digest-pinned published manifest. #2446 proves `weed mini`,
readiness, and the six required S3 behaviors on amd64, arm64, and ppc64le before #2445 can make
it the bundled default. Operators retain the old volume and start a distinct empty data volume,
so the decision never implies format compatibility.

## Success

ADR-0647 unambiguously names the source, command, ports, data path, image lifecycle, evidence,
and transition. Its downstream ownership makes #2445 and #2446 implementable without a second
backend choice. These guarantees apply only to KDIVE's bundled paths named above.

## Validation

- ADR and specification contract: task-test-not-applicable — these are normative prose records;
  no repository consumer parses their wording, while `just records` validates ADR structure.
- Source-selection evidence: task-test-not-applicable — a pinned upstream commit and its source
  tree are reviewed evidence, not behavior implemented in this branch.
- Downstream proof inventory: task-test-not-applicable — #2446/#2445 own executable image and
  consumer proof; this branch creates neither executable surface.
