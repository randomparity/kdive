# Reduced #1519 implementation report

## Scope retained

- Investigation-owned immutable external kernel-build generations and `build_ref` selection.
- External completion publication, content identity, exact object-version persistence, and
  same-Investigation reuse through `runs.create`.
- Exact-VersionId validation and reads for install, raw retrieval, debug, crash, and drgn paths.
- Agent-visible build expiry/deadline contracts and same-Investigation reclaimed-handle tombstones.
- Reusable-generation reclamation with exact-version deletion, retry backoff, bounded/fair
  `expired` and `closed` cursor lanes, and queued/running install-job admission pins.

## Commits added after the reduced Task 1-6 baseline

- `d2ad27a31` fix: pin reusable artifact reads to versions
- `1140749e3` fix: preserve reusable build content identity
- `450d35424` fix: persist reusable artifact version pins
- `eaaa59118` fix: complete reusable build deadline contracts
- `e109afee0` docs: require exact-version object reads
- `08f31ee2c` fix: bound investigation build garbage collection
- `f374a818d` fix: pin reusable artifact reads to versions
- `30682bc11` fix: bind build validation to object versions
- `672116c0f` docs: refresh reusable build run reference
- `ad51e7093` fix: preserve reclaimed build expiry recovery
- `a38b7c244` docs: bound reusable build lifecycle scope

## Verification

- Focused schema/catalog/create/GC proof: 147 passed.
- Broad changed-surface proof: 1,118 passed, 1 live-VM environment skip.
- `just lint`: passed.
- `just type`: passed.
- `just schema-guard`: passed.
- `just migration-order-check`: passed.
- `just docs-check`: passed.
- `just cli-verbs-check`: passed.
- Commit hooks, including migration immutability and `ty`: passed on every commit.

## Explicit exclusions

- No `investigation_build_uses` table or build-use service.
- No worker-incarnation registry or worker-death/recovery tools.
- No Docker lifecycle gate, Docker inspection service, Kubernetes termination witness/finalizer,
  controller, RBAC, or deployment-controller changes.
- No generic legacy/public artifact-GC cursor expansion from the parked worker-lifecycle branch.
- No claim that queued/running job state proves safety across SIGKILL or provider threads that
  outlive job state. That platform boundary is linked to #1803 in ADR-0531 and the design spec.

## Migration tail

- `0095_investigation_builds.sql`
- `0096_investigation_build_gc.sql` (`reclaim_retry_at`, `expired`/`closed` reusable-build cursors)
- `0097_investigation_build_tombstones.sql`

## Reduced-branch review fixes

- `367bb90e1`: reusable Runs now fail before provider invocation unless every referenced artifact
  has a nonempty persisted VersionId; legacy Runs retain key-only fallback.
- `1fd27aecd`: install and get/list retain the full expiry contract after catalog reclamation by
  consulting the persisted build step and same-Investigation tombstone.
- `0f02554c0`: migration 0095 refuses to run with other database clients connected; operator and
  Helm documentation require stop-old-first for the strict Run projection boundary.
- `69c629c53`: removed the transient implementation plan while retaining ADR-0531 and its spec.
- Review-fix focused suite: 595 passed, 1 live-VM environment skip. Lint, type, schema,
  migration-order, docs, CLI, and commit-hook checks passed.
