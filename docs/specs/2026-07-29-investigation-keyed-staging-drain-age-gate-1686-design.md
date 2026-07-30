# Investigation-keyed staging-drain age gate (#1686)

- **ADR:** [`../adr/0501-investigation-keyed-staging-drain-age-gate.md`](../adr/0501-investigation-keyed-staging-drain-age-gate.md)
- **Issue:** #1686 — *staging-drain lane's `systems.created_at` age gate leaves a reused long-staged
  checksum unretried for up to 30 days*

## Problem

`sweep_unowned_investigation_rootfs_staging` (ADR-0494 §5) is the **only** retry for the drained half
of the uploaded-rootfs staging leak — an `open`/`active` investigation with zero rootfs `artifacts`
rows. Its worklist gated on `s.created_at < now() - retention`, per `systems` row.

Content-addressed reuse (ADR-0441) breaks that key. The base lives at
`<uploads>/<inv>/<token>.qcow2`, addressed by content, so a System created minutes ago legitimately
attaches to a checksum staged months ago. The bytes are past retention; the referencing row is not.

Reachable end to end:

| # | Step | Mechanism |
|---|---|---|
| 1 | Last rootfs `artifacts` row drains while the only `upload`-profile System is young | an uploaded-rootfs row can outlive any System — upload precedes provision |
| 2 | A live-held `<token>.<uuid>.partial` is left behind | ADR-0495's disclosed residual: a fetcher that resolved its row but has not yet created its partial is invisible to the `flock` probe |
| 3 | The drain tail defers on the partial and **clears the marker** | ADR-0452 §5 |
| 4 | Nothing re-triggers | ADR-0495's retained-row retry needs a row (there is none); this lane needs a System past retention |

Retry interval degrades from `ROOTFS_STAGING_DRAIN_BACKOFF` (6h) to
`investigation_rootfs_retention` (30d) — ~120x.

The gate was not arbitrary: ADR-0494's Consequences say it "keeps the lane off a System that is
staging its base right now, between the `mkdir` and the row resolution". That window is still live
(#1558 is open; ADR-0495 implemented its option 1 without closing it). But the gate did the job
badly — under a past-retention *sibling* System it admits the job while another System of the same
investigation is mid-`mkdir`, and the tail then sweeps the one directory they share.

## Behaviour

`_UNOWNED_STAGING_INV_SQL` changes in exactly two predicates. Everything else about the lane — the
`systems`-keyed worklist, the profile predicate, the `artifacts` anti-join, the empty `artifact_ids`,
the dedup key, `ROOTFS_STAGING_DRAIN_BACKOFF`, the `repair_kind`, the sweep's signature — is
untouched.

1. `s.created_at < now() - %s` → `i.created_at < now() - %s`.
2. A second anti-join: `AND NOT EXISTS (SELECT 1 FROM systems m WHERE m.investigation_id =
   s.investigation_id AND m.state = ANY(%s))`, with the parameter bound from
   `_MID_MATERIALIZE_STATE_VALUES`.

`_MID_MATERIALIZE_STATE_VALUES` is derived from `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`
(`domain/capacity/state.py`) — `provisioning`, `reprovisioning`, `restoring` — sorted so the bound
parameter is deterministic. Derived rather than restated so the lane cannot drift from the reclaim's
own pin gate, and so `test_reclaim_classification_is_exhaustive` covers it.

The anti-join is **investigation**-scoped, not per-row: the job carries an empty worklist and the
tail sweeps one per-investigation directory, so a settled sibling must not re-admit it.

All comparisons stay against Postgres `now()` in SQL. No Python-side clock.

## Non-goals

- No schema change. `investigations.created_at timestamptz NOT NULL DEFAULT now()` is in
  `0001_init.sql`.
- No `loop.py` / reconciler-catalog change — the sweep's signature is unchanged.
- Not #1558. The state-column blind spot (a `torn_down`/`failed` System whose detached download is
  still writing) needs its option 2 classifier; the `flock` gates remain the defence there.

## Test plan

`tests/reconciler/test_gc_investigation_rootfs.py`. `_seed_investigation` gains an `age` parameter
(default zero) driving `investigations.created_at`; every staging-drain test is aged past retention so
each assertion has exactly one cause rather than passing on a young investigation.

| Test | Asserts | Before |
|---|---|---|
| `..._retries_a_long_staged_investigation_whose_system_is_young` | aged investigation + 1-minute-old `ready` System + no rows → 1 job | **RED** (0) |
| `..._excludes_a_whole_investigation_not_just_the_busy_system` | past-retention `torn_down` sibling + `provisioning` System → 0 | **RED** (1) |
| `..._leaves_an_investigation_with_a_mid_materialize_system_alone[3 params]` | each pre-overlay state excludes the investigation | green for the old reason; reddens when the anti-join is removed |
| `..._leaves_a_young_investigation_alone` | young investigation → 0 | pins the new gate's other direction |
| `..._mid_materialize_states_are_the_curated_pre_overlay_set` | the constant equals ADR-0441 §6's set | drift guard |

The anti-join's four assertions were verified to redden by neutralising the predicate, not assumed to.
