# Reopen the worker claim path for authority-marked external-boot payloads

Issue: [#2201](https://github.com/randomparity/kdive/issues/2201), part of #2118.
Governing decision: ADR-0584 (provider-host authority fences external-boot mutations).
Debt record: `docs/debt/0003-external-boot-contracts-await-their-executor.md`.

## Goal

Make a job whose payload carries the `external_boot_authority_v1` key claimable by a worker
and visible to the queue-depth count, while keeping `commit_external_boot_authority_result`
the only path that can terminalize such a job. That takes two things, not one: leaving the
generic finalization fence installed, **and** adding the payload predicate that
`repair_abandoned_jobs` never needed while these jobs were unclaimable.

## Context

`0122_external_boot_authority.sql:275-320` contains a single `DO` block that rewrites four
functions from `pg_get_functiondef`, splicing an exclusion into each and re-executing the
result. The block's comment states the intent: "Marked work is installed but deliberately not
enabled for claim or generic finalization." That was correct while no executor existed.

The block has two branches over one array of four regprocedures, and they are not
interchangeable:

| | claim branch (`:293-303`) | finalizer branch (`:304-315`) |
|---|---|---|
| functions | `claim_worker_job(text,bytea,interval,text[])`, `count_claimable_worker_jobs(text[])` | `complete_worker_job(uuid,bytea,integer,text)`, `fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)` |
| anchor marker | `AND j.attempt < j.max_attempts` | `AND state = 'running'` |
| injected text | `AND NOT (j.payload ? 'external_boot_authority_v1')` — alias-qualified | `AND NOT (payload ? 'external_boot_authority_v1')` — unqualified |
| placement | on its own line **before** the marker | appended **after** the marker |
| this change | reversed | left installed |

The claim branch is the single gate that makes every external-boot job unclaimable. A handler
registered for an operation can never run, because no worker can take the job and
`count_claimable_worker_jobs` does not see it either. Reopening it is the whole of this
change.

The finalizer branch must stay. `commit_external_boot_authority_result`
(`0122...sql:731`, returning `(status text, job_state text)` at `:749`) writes the `jobs` row
itself under `SECURITY DEFINER`. Reopening the generic finalizers would give an
authority-marked job two competing terminalization paths, one of them outside that commit.

## Decision

Add `src/kdive/db/schema/0127_reopen_external_boot_claim_lane.sql` (number assigned by the
dispatching orchestrator; `0126_remote_module_attempt_obligations.sql` is the highest on
`main`). It reverses only the claim branch, using the same
`pg_get_functiondef` + `replace` + `EXECUTE` idiom `0122` used, and then asserts the
finalizer branch is still installed.

### Reverse on the full injected text, not on the token

The two injections differ only by an alias prefix. A reversal that matched the bare
`external_boot_authority_v1` token, or that matched `AND NOT (payload ? ...)`, would either
over-match into the finalizer pair or silently no-op. So the migration removes the exact
string `0122` inserted, newline and trailing indentation included:

```
E'AND NOT (j.payload ? ''external_boot_authority_v1'')\n          '
```

`0122` inserted that immediately before `AND j.attempt < j.max_attempts`, which itself sat at
ten spaces of indentation, so removing exactly that string restores the pre-`0122` body
byte-for-byte. The migration requires the injected text **and** its anchor marker to be
adjacent before replacing, which is a stronger shape check than either alone.

### Fail loudly, never no-op

`0122:289-292` refuses when the exclusion is already present. The mirror for a reversal is to
refuse when it is *not* present, and this migration adds the other end too:

1. **Precondition** — the injected text immediately followed by its anchor marker must be
   present in each claim-side definition. Absent, raise.
2. **Postcondition** — after the replacement, the definition must contain no occurrence of
   `external_boot_authority_v1`. Present, raise.
3. **Non-regression** — after the claim-side rewrite, both finalizer definitions must still
   contain `external_boot_authority_v1`. Absent, raise.

Checks use `position(... in ...)` rather than `LIKE`. Both the anchor marker
(`max_attempts`) and the token (`external_boot_authority_v1`) contain `_`, which is a `LIKE`
single-character wildcard; `0122`'s own `LIKE` checks are correspondingly loose. Exact
substring search removes that looseness in both directions.

### Idempotency

`src/kdive/db/migrate.py` is a forward-only ledger runner (ADR-0015): it records each applied
file in `schema_migrations` and skips an already-recorded version after verifying its
checksum. Re-running the suite is therefore a no-op at the runner, and the migration body
executes exactly once. Re-executing the body by hand against an already-reversed database
raises on precondition 1 — the same fail-loud posture `0122` has, and the intended one.

### What does not change

No role grant, no table privilege, no `p_purpose` value, no `JobKind` member, no CHECK
constraint. `CREATE OR REPLACE FUNCTION` preserves the existing ACL and owner, so the
`0116_capture_claimable_queue_depth.sql:53-56` grants on `count_claimable_worker_jobs` and
the `0122...sql:1738-1748` grants on the two authority functions survive untouched. Nothing
in `src/kdive/jobs/` or `src/kdive/domain/operations/jobs.py` is edited.

## Considered & rejected

- **Match and remove the bare `external_boot_authority_v1` token.** verified: the token
  occurs in all four definitions after `0122` (`0122...sql:302` and `:314` inject it), so a
  token-level removal reaches the finalizer pair — the exact over-match this design's alias
  analysis identified before any SQL was written.
- **`CREATE OR REPLACE FUNCTION` with the pre-`0122` body copied from
  `0112_capture_operation_supervision.sql:1244-1342` and
  `0116_capture_claimable_queue_depth.sql:5-51`.** judgment: it re-pins a body that later
  migrations may have amended for unrelated reasons, so a future migration touching either
  function would be silently reverted by a replay of this one. Deriving from
  `pg_get_functiondef` keeps the reversal orthogonal to whatever else the body has accrued.
- **Drop the fence for all four functions and rely on
  `commit_external_boot_authority_result` being the only caller in practice.** verified:
  `0122...sql:731,749,1661,1681` shows that function writing the `jobs` row itself, so the
  generic finalizers would be a second terminalization path reachable by any worker holding
  the job's credential — a race outside the `SECURITY DEFINER` commit rather than a
  hypothetical one.
- **Do nothing and let the sibling issue's handlers carry a bespoke claim path.** judgment:
  it duplicates the queue's only claim primitive for one job family and leaves
  `count_claimable_worker_jobs` still blind to the work, so queue-depth telemetry would
  under-report exactly the backlog the executor exists to drain.

## Threat model

This change is security-relevant: it redefines two `SECURITY DEFINER` functions and widens
what a worker may claim.

**Boundaries.** No boundary is added. One existing boundary is widened: the set of `jobs`
rows a caller of `claim_worker_job` / `count_claimable_worker_jobs` can reach now includes
authority-marked rows.

**Actors.** The only actor that reaches either function is a process holding the
`kdive_worker` role — a deployment-operated worker, not an end user or tenant. `kdive_server`
is explicitly revoked from the authority functions (debt record 0003 states this), and both
claim functions begin with `pg_has_role(session_user, 'kdive_worker', 'member')` raising
`42501` otherwise.

**Controls per boundary.**

- *Who may claim* — unchanged: the `pg_has_role` check at
  `0112...sql:1259-1261` and `0116...sql:12-14` is preserved verbatim by the reversal, since
  the replacement touches only the `WHERE` predicate.
- *Which row a claim may take* — the credential-bound worker-incarnation check
  (`0112...sql:1269-1286`) and `FOR UPDATE ... SKIP LOCKED` are likewise untouched.
- *Who may terminalize an authority-marked job* — the finalizer fence, retained and asserted
  by this migration's third guard and by a test, **plus** an explicit payload predicate added
  to `repair_abandoned_jobs` (`src/kdive/reconciler/repairs/jobs.py`). See below: the fence
  alone is not sufficient once this change lands.
- *What the widened set exposes* — nothing new. A worker already had `SELECT` on `jobs`; the
  fence governed claimability, not visibility.

**The reconciler path this change opens, and closes.** The claim "the finalizer fence leaves
`commit_external_boot_authority_result` as the only path that can terminalize an
authority-marked job" is true today only because such jobs cannot be claimed, so they never
acquire a lease that can expire. `repair_abandoned_jobs`
(`src/kdive/reconciler/repairs/jobs.py:25-74`) is raw SQL that 0122 never fenced: it selects
`jobs WHERE state = 'running' AND lease_expires_at < now() AND attempt >= max_attempts`, sets
them `failed` with `lease_expired`, and drives the owning Run to `failed` — all outside the
`SECURITY DEFINER` commit. Reopening the claim lane is what makes that path reachable, so this
change adds the missing predicate there rather than leaving the window open. The change that
opens a window closes it.

**Accepted consequence: a leak window until #2203.** Skipping these jobs is the correct trade —
an unfenced terminalization that corrupts authority state is worse than a job that lingers —
but it is a visible leak, not a neutral choice. After this change, an authority-marked job
whose worker dies sits `running` with an expired lease and **nothing reaps it**. It stays that
way until #2203 adds the reconciler detection lane that routes it to the authority path
instead of writing to it directly. #2203 is the right owner: `0121...sql:280-285` grants
`kdive_reconciler` `SELECT` only on the external-boot tables, so the reconciler can detect and
enqueue but cannot itself commit an authority result. This window is accepted deliberately and
recorded here so it is read rather than discovered.

**Out of scope.** Authority allocation and commit semantics (ADR-0584, already accepted and
unchanged here); the payload shape and handlers that will populate
`external_boot_authority_v1` (sibling issue); the reconciler detection lane that will drain
the leak above (#2203 — this change adds one predicate to `repair_abandoned_jobs` and no
detection logic); the `SELECT`-only grants at `0121...sql:280-285` (asserted unchanged, not
modified).

## Testing

All tests run against the disposable-Postgres fixture with the full migration set applied.

1. **Claim-side reversal** — `pg_get_functiondef` for `claim_worker_job` and
   `count_claimable_worker_jobs` contains no occurrence of `external_boot_authority_v1`.
2. **Finalizer non-regression** — `pg_get_functiondef` for `complete_worker_job` and
   `fail_worker_job` still contains it. This is the test that catches an over-matching
   reversal, and it is a required deliverable rather than a nicety.
3. **Behavioral claim** — a seeded `jobs` row whose payload carries the key is returned by
   `claim_worker_job` and counted by `count_claimable_worker_jobs`.
4. **Behavioral finalizer refusal** — `complete_worker_job` and `fail_worker_job` against
   that same running job leave it non-terminal.
5. **Fail-loud precondition** — re-executing the migration body against the already-reversed
   database raises rather than no-opping.
6. **Grant and vocabulary non-regression** — the two authority functions are still executable
   by `kdive_worker` alone; the `0121` `SELECT`-only grants are intact; the `p_purpose` and
   `v_job.kind` CHECK constraints are unchanged.
7. **Idempotency** — a second `apply_migrations` call returns no newly applied versions.

8. **Reconciler non-regression** — `repair_abandoned_jobs` leaves an authority-marked job with
   an expired lease and exhausted attempts `running`, while still reaping an unmarked one in
   the same sweep.

Test 2 is proven to bite by deliberately extending the migration's reversal to the finalizer
pair and observing the assertion fail, then reverting. Test 8 is proven to bite by removing
the payload predicate from `repair_abandoned_jobs` and observing the marked job terminalized.
