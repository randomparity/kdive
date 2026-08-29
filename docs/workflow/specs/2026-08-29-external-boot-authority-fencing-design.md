# External-boot authority fencing design

## Scope

Issue #2125 implements the database and worker-finalization slice of accepted
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md). Migration
0122 adds security-definer contracts that allocate monotonic per-System authority generations from
an authenticated exact running job attempt, record provider acknowledgements, and commit core truth
only for the matching current generation. Provider-host transport, journals, deployment ACLs, and
live-provider proofs remain separate work.

The implementation supports Python 3.14 and the repository's x86_64 and ppc64le targets. PostgreSQL
is the source of lifecycle truth. Credentials are accepted only as hashes inside security-definer
functions and are never persisted in authority or audit rows.

## Architecture

Migration 0122 owns five durable records. `external_boot_authority_counters` keeps the last issued
generation per System and is never decremented or deleted by application roles.
`external_boot_authorities` stores the immutable System, Allocation, activation, Run, plan, job,
attempt, purpose, provider, authority-instance, worker-incarnation, operation identity, and digest
binding plus its `allocating|current|superseded|retired` state. A partial unique index permits one
current row per System. `external_boot_authority_journal_heads` stores the trusted
per-authority-instance/System journal sequence, record digest, phase, and operation identity. The
provider role advances it by monotonic compare-and-set from the exact prior sequence and digest;
neither truncation nor a longer uncommitted suffix can move it.
`external_boot_authority_acknowledgements` stores the provider-authority principal's
acknowledgement, journal sequence/digest, operation digest, and positive-quiescence digest, and
promotion requires an exact match to the trusted head. `external_boot_authority_audit` stores
takeover and commit outcomes with identity references only. Allocation creates a new lane's sole
genesis head, bound to the System and authority instance, at sequence zero with the protocol's fixed
empty-journal SHA-256 digest and phase `empty`. The provider role has no direct table INSERT grant;
its first append must compare-and-set that exact genesis head to sequence one and `admitted`.
CAS enforces the closed phase graph `empty -> admitted -> mutation-started -> provider-returned ->
observed -> terminal`; it permits no skips, repeats, or backward edges. System, authority instance,
generation, and operation identity are immutable across the lane, and each new digest must bind the
prior digest and the complete immutable lane identity.

`allocate_external_boot_authority` authenticates the worker credential, resolves project and
Allocation from the activation without trusting that first read, then acquires advisory locks in the
repository-wide `Project -> Allocation -> System -> Run` order. Under those locks it re-reads and
requires the Allocation to be active, the System and Run to be in the purpose-specific admissible
states, and the exact job attempt still running. It requires the locked job to carry a versioned persisted
`external_boot_authority_v1` admission marker plus the exact activation, Run, System, plan, purpose, provider,
authority-instance, operation identity, and operation digest binding. It increments the counter,
supersedes any current authority, inserts the allocating binding, and appends a takeover audit record
in one transaction. The generation is database-generated and never caller-selected.
The purpose-to-kind mapping is closed: `activate`, `recover`, `resolve-conflict`, and `release` use
`boot` jobs whose admission marker names that purpose; `teardown` uses a `teardown` job. Reconcilers
must enqueue one of those durable jobs and cannot allocate or commit directly. Every retry or later
Run is a newly charged exact attempt and receives a distinct generation.

`acknowledge_external_boot_authority` is callable only by the provider-authority role. It locks the
System and authority, requires the complete immutable binding and positive quiescence fields, writes
the acknowledgement once, and promotes only that allocating generation to current. Replays with
identical facts are idempotent; mismatches affect zero rows.
An identical replay after promotion returns `applied` with the existing acknowledgement, including
after response loss; a concurrent or later replay with any differing fact returns `superseded` and
does not alter the current row or trusted head.

`commit_external_boot_authority_result` authenticates the worker credential and takes advisory
locks in System then Run order before row-locking the job, authority, activation, current recovery
attempt, acknowledgement, and journal head in that order. Allocation takes the System advisory
lock before the job and activation rows; acknowledgement takes the System advisory lock before
authority, head, and acknowledgement rows. It validates the exact current
generation, acknowledgement, job attempt ownership, activation/Run/System/plan binding, purpose,
provider, authority instance, journal sequence/digest, and operation digest. Its requested operation
is a closed enum covering activation state/evidence transition, activation deadline update,
recovery-attempt creation or state/evidence transition, exact job completion, exact job
failure/requeue with Run compensation, reservation release, and cleanup completion. Each variant
accepts only the columns it owns, checks the legal lifecycle edge, and commits its mutation and audit
row atomically. Any mismatch returns `superseded` without changing lifecycle, job, cleanup, or audit
rows. Existing direct repository methods remain server-only admission/preparation operations;
actor-originated provider results must use this contract and cannot fall back to them.

The persisted job payload carries only immutable pre-claim admission facts: the external marker,
activation, Run, System, plan, purpose, provider, authority instance, operation identity, and
operation digest. It never carries a generation or authority reference. After claim, allocation
returns the database-generated authority UUID and generation; the worker combines those with the
unchanged admission facts in the provider request and final result carrier. Handlers return either
the existing `str | None` result or a typed
`ExternalBootAuthorityResultV1`. Its success and failure variants share a literal schema
discriminator and mandatory
activation, System, Run, plan, authority-reference, generation, purpose, provider,
authority-instance, operation, journal sequence/digest, operation digest, and result reference
fields. The failure variant additionally carries the stable error category and the bounded, redacted
failure context. Provider adapters attach the immutable authority facts to categorized exceptions,
so response loss, timeout, and provider rejection reach the same authority-bound failure path. The
boot handler must return or raise the typed carrier whenever the Run's durable boot contract is
external; missing or malformed authority facts leave the marked job running for reclaim and emit
only a bounded local diagnostic, with no generic completion/failure. The Python queue layer accepts
only the typed carrier for authority-bound completion/failure.

migration 0122 changes the existing worker-authentication, claim, heartbeat, complete, fail, and
related active-incarnation functions from exact protocol 3 to minimum supported protocol 3, while
making generic complete/fail affect zero rows for a job carrying the external marker. Enqueue of
marked jobs stays disabled until active worker incarnations advertise fence protocol 4; claim
excludes marked jobs for older protocols. Protocol-4 workers process ordinary jobs through the
compatible generic functions. Deployment is
migration first, then protocol-4 workers, then external enqueue enablement. Rollback disables new
external enqueue but never re-enables generic finalization for an already-marked job. All other job
kinds retain their existing exact-attempt fence.

Every text input is measured in UTF-8 bytes before mutation. Provider kind, purpose, phase, and
operation are closed enums; authority instance and operation identity are 1–255 bytes; opaque
authority references are UUIDs; plan, operation, record, previous-record, and quiescence digests use
`sha256:` plus 64 lowercase hex digits; result references are null or 1–2048 bytes; failure context
is a JSON object of at most 32 string entries, each key 1–64 bytes and value at most 1024 bytes, with
total PostgreSQL size at most 32 KiB; lifecycle evidence is a JSON object at most 64 KiB. Journal
sequence is positive and must increase by exactly one. Audit outcome text is a closed enum and audit
rows carry no caller-supplied free text.

Lifecycle evidence is not generic JSON. Each operation accepts the existing versioned ADR-0583
evidence schema for that state edge and verifies its required ownership, outcome, identity, and
state-specific fields in SQL before mutation. The operation digest binds canonical JSON (sorted
object keys, UTF-8, no insignificant whitespace) of the immutable operation and evidence identity;
the security-definer function recomputes it. A bounded object that is missing a required key,
contains a foreign ownership identity, names the wrong outcome, or hashes differently raises
`22023` before any durable write.

## Failure contract

- Invalid field shape or size raises SQLSTATE `22023` before mutation.
- A caller lacking the worker or provider-authority role raises SQLSTATE `42501`.
- A stale, cross-System, cross-Run, cross-activation, cross-attempt, wrong-purpose,
  wrong-provider, wrong-authority-instance, or acknowledgement mismatch returns `superseded` and
  affects zero durable rows.
- Counter overflow raises `22003`; generations are never wrapped or reused.
- A missing, reordered, truncated, divergent, foreign, or non-monotonic journal head affects zero
  rows. An acknowledgement without positive quiescence or an exact trusted journal-head match cannot
  promote authority.
- Audit rows contain UUIDs, bounded digests, generations, purpose, and outcome; they contain no
  credentials, provider definitions, commands, paths, or provider secrets.

## Threat model

The added worker-to-database boundary accepts a credential, job/attempt identity, and immutable
authority binding from an authenticated worker. The security-definer functions hash and authenticate
the credential through the existing active-incarnation contract, validate every identifier against
locked database truth, bound text/JSON inputs, and return only an opaque status on mismatch. An
authenticated worker for another project, System, Run, activation, or attempt is untrusted.

The added provider-authority-to-database boundary accepts acknowledgement and quiescence evidence
from the dedicated database role. Role membership, exact authority-instance and operation binding,
positive journal sequence, fixed-format digests, and an allocating authority row control it. The
role cannot allocate authority or update job, Run, System, activation, cleanup, or accounting rows.

Database administrators and privileged provider-host administrators remain trusted and can bypass
SQL or provider ACLs; ADR-0584 explicitly excludes that bypass from generation fencing. Provider
journal integrity and mutation serialization are outside this slice and must be proved by the
provider-host authority implementation before external-boot v1 is advertised.

## Verification

Migration tests race allocations and prove strictly increasing generations. They exercise every
purpose-to-job-kind mapping; pre-claim admission to post-claim authority handoff; genesis journal
head initialization, first CAS, restart, truncation, and foreign-head rejection; every cross-binding
mismatch; inactive credentials; acknowledgement mismatch; stale takeover; exact idempotent replay;
later-Run denial; cleanup and audit zero-row behavior; old-worker generic-finalization rejection;
malformed success/failure carriers; semantically invalid evidence; grants; and absence of credentials
or provider secrets in persisted rows. They also prove the complete journal phase graph rejects
skips, repeats, foreign identities, and cross-operation heads; allocation racing Allocation release
mints no authority after release begins; and protocol-4 workers claim, heartbeat, complete, retry,
and fail ordinary jobs. Queue/worker tests prove external-boot success and failure
finalization use the authority-bound contract and stale results are dropped, while ordinary jobs
keep the existing path. `just lint`, `just type`, focused database/job tests, and `just ci` remain
the guardrails.
