# Authority journal headroom for teardown (#3526)
## Problem

A retained-quarantine provision receipt requeues its job and raises `max_attempts` by one
(`0149_authority_owned_system_provisioning.sql`, `finalize_authority_system_attempt`), so a
provision that fails on every attempt has no retry bound. Each attempt appends up to eight records
to the System's authority journal. After about 128 attempts the journal reaches
`MAX_RECORDS_PER_JOURNAL` (1024), the teardown takeover cannot append, and the System keeps its
allocation. The retry source is SQL; the shared, bounded resource is the journal.

## Decision

Reserve journal headroom for teardown; do not bound the retry. Bounding the retry means changing
the SQL finalizer (a migration, and it would also stop legitimate retries of transient
provider failures). A reserve is Python-only and keeps retries unbounded while they are cheap.
Rejected: raising or removing the journal bound (excluded by the operator).

## Scope

- `journal.py` adds `RECORDS_PER_ATTEMPT` (8, the longest attempt), `TEARDOWN_RESERVE_RECORDS`
  (four attempts) and `PROVISION_RECORD_CEILING` (1024 minus the reserve). Bounds, validation and
  the hash chain are unchanged.
- `AuthoritySystemService._begin` refuses a new provision attempt with `journal-conflict` when
  the attempt would not fit under the ceiling. The check runs only before the first record of a
  new attempt, so a started attempt always finishes. Teardown is never refused by it.
- The refusal reuses the existing wire category, so there is no protocol change. The job fails
  retryably, exhausts its attempts without growing `max_attempts`, and `systems.teardown`
  takes over; migration 0171 then discharges the exhausted provision job.
- Out of scope: a teardown that itself retains forever (it can still consume the reserve), the
  SQL retry rule, journal format.

### Failure model

- Actors: workers calling the authority service; one lane per System under a lock.
- Invariants: provision never writes past the ceiling; teardown can append while the reserve lasts.
- Accepted: a System whose provision fails persistently stops retrying near 990 records and
  stays `provisioning` until torn down; a reserve of four teardown attempts can be spent by a
  teardown that also fails persistently.
- Covered elsewhere: exhausted provision discharge (0171); journal integrity checks (journal.py).

## Success

- A persistently failing provision is refused at the ceiling, then teardown completes.
- The journal bounds are unchanged.

## Validation

- Drive failing provisions to the refusal, then a teardown to a terminal receipt, against the
  real file journal: focused-test, `tests/providers/system_authority/test_service.py`.
- Bounds unchanged: focused-test, same file.
- Fault: disabling the guard reproduces `exceeds 1024 records`: controlled-fault.
- Live job retry path: task-test-not-applicable; the refusal is an ordinary retryable failure,
  whose exhaustion and discharge existing migration 0171 tests cover.
