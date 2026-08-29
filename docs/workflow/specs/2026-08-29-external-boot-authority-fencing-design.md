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

Migration 0122 owns four durable records. `external_boot_authority_counters` keeps the last issued
generation per System and is never decremented or deleted by application roles.
`external_boot_authorities` stores the immutable System, Allocation, activation, Run, plan, job,
attempt, purpose, provider, authority-instance, worker-incarnation, operation identity, and digest
binding plus its `allocating|current|superseded|retired` state. A partial unique index permits one
`current` authority per System.
`external_boot_authority_acknowledgements` stores the provider-authority principal's
acknowledgement, journal sequence/digest, operation digest, and positive-quiescence digest, and
promotion requires an exact match to the newest allocating authority's immutable binding.
`external_boot_authority_audit` stores takeover and commit outcomes with identity references only.
Journal anchoring, phase continuity, append/fsync, restart, and provider-call quiescence proof belong
to #2126; migration 0122 treats the provider-role acknowledgement's bounded journal and quiescence
digests as opaque evidence and never claims to validate the journal behind them.

`allocate_external_boot_authority` authenticates the worker credential, resolves project and
Allocation from the activation without trusting that first read, then acquires advisory locks in the
repository-wide `Project -> Allocation -> System -> Run` order. Under those locks it re-reads and
requires the Allocation to be active, the System and Run to be in the purpose-specific admissible
states, and the exact job attempt still running. It requires the locked job to carry a versioned persisted
`external_boot_authority_v1` admission marker plus the exact activation, Run, System, plan, purpose, provider,
authority-instance, operation identity, and operation digest binding. It increments the counter,
supersedes every prior `allocating` or `current` authority for the System, inserts the allocating
binding, and appends a takeover audit record in one transaction. The generation is database-generated
and never caller-selected.
The purpose-to-kind mapping is closed: `activate`, `recover`, `resolve-conflict`, and `release` use
`boot` jobs whose admission marker names that purpose; `teardown` uses a `teardown` job. Reconcilers
must enqueue one of those durable jobs and cannot allocate or commit directly. Every retry or later
Run is a newly charged exact attempt and receives a distinct generation.

`acknowledge_external_boot_authority` is callable only by the provider-authority role. It locks the
System and authority, requires the complete immutable binding and positive quiescence fields, and
requires the generation to equal the System counter's latest value before writing the
acknowledgement and promoting that allocating generation to current. A delayed acknowledgement for
any older generation returns `superseded`. Replays with
identical facts are idempotent; mismatches affect zero rows.
An identical replay after promotion returns `applied` with the existing acknowledgement, including
after response loss; a concurrent or later replay with any differing fact returns `superseded` and
does not alter the current row or acknowledgement.

`commit_external_boot_authority_result` authenticates the worker credential and takes advisory
locks in System then Run order before row-locking the job, authority, activation, current recovery
attempt and acknowledgement in that order. Allocation takes the System advisory
lock before the job and activation rows; acknowledgement takes the System advisory lock before
authority and acknowledgement rows. It validates the exact current
generation, acknowledgement, job attempt ownership, activation/Run/System/plan binding, purpose,
provider, authority instance, acknowledged journal sequence/digest, and operation digest. Its requested operation
is a closed enum covering activation state/evidence transition, activation deadline update,
recovery-attempt creation or state/evidence transition, exact job completion, exact job
failure/requeue with Run compensation, reservation release, System teardown transition, and cleanup
completion. The teardown variant requires terminal provider evidence under the current `teardown`
generation and changes `SystemState` to `torn_down` only in the same transaction as the accepted job
result and audit row; the handler performs no pre-provider terminal transition. Each variant
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
fields. Each success/failure operation variant also carries its mandatory versioned ADR-0583
lifecycle-evidence object: activation materialization/terminal evidence, recovery conflict/terminal
evidence, release evidence, cleanup evidence, or teardown terminal evidence as appropriate. There is
no generic evidence variant and a result reference cannot substitute for evidence. The failure
variant additionally carries the stable error category and the bounded, redacted failure context.
Provider adapters attach the immutable authority facts and operation-specific evidence to categorized exceptions,
so response loss, timeout, and provider rejection reach the same authority-bound failure path. The
boot handler must return or raise the typed carrier whenever the Run's durable boot contract is
external; missing or malformed authority facts leave the marked job running for reclaim and emit
only a bounded local diagnostic, with no generic completion/failure. The Python queue layer accepts
only the typed carrier for authority-bound completion/failure.

migration 0122 preserves migration 0113 and ADR-0559's exact protocol-4 worker-authentication,
claim, heartbeat, completion, failure, capture, and publication gates. It changes only generic
complete/fail so they affect zero rows for a job carrying the external marker. Enqueue of marked
jobs remains unavailable in this slice. The later provider-host authority/deployment owner must drain
or replace incompatible workers and explicitly enable external enqueue only after installing the
mutation authority; migration 0122 supplies no readiness switch that could be enabled prematurely.
Protocol-3 incarnations remain unable to authenticate or claim any work. Rollback never re-enables
generic finalization for an already-marked job. All ordinary jobs retain their existing exact-attempt
fence and behavior.

Every text input is measured in UTF-8 bytes before mutation. Provider kind, purpose, and operation
are closed enums; authority instance and operation identity are 1–255 bytes; opaque authority
references are UUIDs; plan, operation, journal, and quiescence digests use `sha256:` plus 64 lowercase
hex digits; result references are null or 1–2048 bytes; failure context
is a JSON object of at most 32 string entries, each key 1–64 bytes and value at most 1024 bytes, with
total PostgreSQL size at most 32 KiB; lifecycle evidence is a JSON object at most 64 KiB. The opaque
journal sequence is positive; #2126 owns continuity validation. Audit outcome text is a closed enum
and audit rows carry no caller-supplied free text.

Lifecycle evidence is not generic JSON. Each operation accepts the existing versioned ADR-0583
evidence schema for that state edge and verifies its required ownership, outcome, identity, and
state-specific fields in SQL before mutation. The allocation function, not Python or a provider,
mints the operation digest from the locked relational binding using one PostgreSQL expression over
fixed text/UUID/integer fields and stores it on the authority row. The returned digest is an opaque
identity that provider acknowledgements and result commits must echo exactly; neither side
re-serializes lifecycle evidence to recompute it. Evidence is validated directly from parsed
`jsonb`, whose parser has already rejected duplicate keys into one deterministic value, and its
state-specific scalar fields are compared with locked relational truth. A bounded object missing a
required key, carrying foreign ownership, naming the wrong outcome, or paired with a different
stored operation digest raises `22023` before any durable write.

## Failure contract

- Invalid field shape or size raises SQLSTATE `22023` before mutation.
- A caller lacking the worker or provider-authority role raises SQLSTATE `42501`.
- A stale, cross-System, cross-Run, cross-activation, cross-attempt, wrong-purpose,
  wrong-provider, wrong-authority-instance, or acknowledgement mismatch returns `superseded` and
  affects zero durable rows.
- Counter overflow raises `22003`; generations are never wrapped or reused.
- An acknowledgement without bounded positive-quiescence evidence, a positive journal sequence,
  fixed-format journal digest, or an exact immutable authority binding cannot promote authority.
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

This issue tests only persistence and exact matching of bounded opaque acknowledgement metadata. It
does not implement or claim proof of provider journal anchoring, phase continuity, append/fsync,
process restart, surviving provider calls, truncation recovery, mutation serialization, deployment
ACLs, or live providers; those belong to #2126/#2127.

## Verification

Migration tests race allocations and prove strictly increasing generations. They exercise every
purpose-to-job-kind mapping; pre-claim admission to post-claim authority handoff; every cross-binding
mismatch; inactive credentials; acknowledgement mismatch; stale takeover; exact idempotent replay;
delayed acknowledgement after a newer allocation; mixed old/new protocol-4 workers and the separate
external-enqueue absence; later-Run denial; cleanup and audit zero-row behavior; old-worker
generic-finalization rejection; foreign authority/generation acknowledgements;
malformed success/failure carriers; semantically invalid evidence; grants; and absence of credentials
or provider secrets in persisted rows. They also prove external teardown cannot set the System
terminal before accepted provider evidence and stale teardown affects zero System/job/audit rows;
opaque acknowledgement metadata is bounded and exactly matched; allocation racing Allocation release
mints no authority after release begins; and protocol-4 workers claim, heartbeat, complete, retry,
and fail ordinary jobs while protocol-3 workers remain rejected from claim, heartbeat, capture,
success, retry, and failure. Queue/worker tests prove external-boot success and failure
finalization use the authority-bound contract and stale results are dropped, while ordinary jobs
keep the existing path. `just lint`, `just type`, focused database/job tests, and `just ci` remain
the guardrails.

Digest tests prove PostgreSQL mints the same value for one immutable relational binding regardless
of Python/provider JSON serialization, rejects any changed binding, and requires exact opaque echo
through acknowledgement and commit.
Carrier tests reject missing, foreign, malformed, wrong-operation, and semantically incomplete
lifecycle evidence before the queue adapter calls SQL.
