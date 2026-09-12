# SeaweedFS bundled S3 backend contract — Implementation Plan

**Goal:** record the selected SeaweedFS backend and the bounded contract that later image and
consumer issues must implement.

**Architecture:** ADR-0647 pins upstream source and specifies a locally built `weed mini`
image. It separates image/proof ownership (#2446) from consumer migration (#2445) and preserves
the old MinIO volume as a rollback artifact.

**Tech stack:** Markdown ADR/spec/plan; SeaweedFS upstream source; existing ADR-0017/0356 rules.

## Global Constraints

- The source revision is `d997fba1575583a89cf0cc50dc0150642286c86d` (SeaweedFS 4.46).
- Bundled paths require linux/amd64, linux/arm64, and linux/ppc64le build-manifest evidence.
- Do not change Python ObjectStore APIs, schema, external S3 contracts, Compose/Helm/fixtures,
  or migrate/reuse MinIO data in this issue.

Expected implementation size: 120–220 changed lines (M) — one ADR contract and its downstream
proof/ownership inventory.

## File map

| File | Responsibility |
| --- | --- |
| `docs/adr/0647-supported-s3-backend-and-compose-data-transition.md` | selected backend and normative transition/proof contract |
| `docs/adr/0639-minio-images-use-official-quay-registry.md` | supersession pointer only |
| `docs/workflow/specs/2026-09-12-seaweedfs-s3-backend-contract-design.md` | frozen decision rationale and failure model |
| `docs/workflow/plans/2026-09-12-seaweedfs-s3-backend-contract.md` | implementation handoff |

## Task 1: Record the decision

**Files:** create ADR-0647; modify ADR-0639 only with its supersession banner.

**Interfaces:** consumes ADR-0017's S3 behavior and ADR-0356's image-matrix posture; supplies
the exact source pin, `weed mini` invocation, image lifecycle, proof requirements, and volume
transition to #2445/#2446.

**Verification:**

- Contract: ADR structure and links. Mode: task-test-not-applicable — this ADR is read by
  maintainers rather than a prose parser; `just records` checks record structure.
- Contract: source evidence. Mode: task-test-not-applicable — Git commit resolution is external
  primary-source evidence, not executable behavior in this repository.

1. Add the proposed ADR with its five required sections.
2. Pin release 4.46's full commit ID and specify `S3_BUCKET=kdive-artifacts weed mini -dir=/data`.
3. State that #2446 proves `weed mini`, readiness, and required S3 behavior on amd64, arm64,
   and ppc64le before #2445 may make the image the bundled default, plus the no-reuse transition.
4. Add only ADR-0639's supersession banner.
5. Run `just records`; expect success.

**Acceptance criteria:** the ADR names no mutable image tag as evidence, assigns all runtime
proof to #2446 before #2445 adoption, and does not claim compatibility proof that #2446 has not run.

## Task 2: Preserve the implementation handoff

**Files:** create the SeaweedFS specification and plan.

**Interfaces:** consumes ADR-0647; supplies the frozen failure model, exclusions, and concrete
proof inventory to #2445/#2446.

**Verification:**

- Contract: scoped design records. Mode: task-test-not-applicable — no executable consumer reads
  these workflow documents; guardrails validate links and ADR record shape.

1. Write the spec's problem, scope, failure model, architecture, success, and validation sections.
2. Write this plan with exact downstream ownership and no implementation tasks outside the ADR.
3. Run `just records`; expect success.

**Acceptance criteria:** every promised downstream proof has one owner, and the documents retain
the operator-authorized exclusions without adding an implementation surface.
