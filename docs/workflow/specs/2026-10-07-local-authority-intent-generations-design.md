# Local authority System intent across attempt generations (#3144)

## Problem

An authority-owned local System provision that does not finish in its first attempt generation
never finishes. Each later generation answers `provider-conflict`. The live log from #3139 shows
`LocalAuthoritySystemError: local authority intent does not match the request`, raised in
`LocalAuthoritySystemProvider._require_matching_intent`.

The field that differs is `operation_digest`. Migration 0149
(`allocate_authority_system_attempt`) hashes the job attempt, the request attempt id, the worker
incarnation, and the generation into the digest, so each generation gets a new value. The
provider writes the private intent once (`O_EXCL`), during the first generation, with that
generation's digest. Every later generation then fails the match:

- generation N+1 `execute_system_provision` fails before it resumes the retained domain;
- the takeover of generation N+2 observes generation N+1's request (`_quiescence`), fails the
  same way, and `acknowledge_system_takeover` answers `provider-conflict`.

The provision and teardown completion receipts (`_complete_provision_facts`,
`_completion_time`, `_execute_teardown`) also compare a stored, per-generation `operation_digest`
with the request's digest. A completion that one generation wrote is then refused by the next
generation.

The remote provider does not have this defect: `RemoteAuthoritySystemProvider._adopt` matches
the System subject and records the new attempt.

## Scope

Change only `src/kdive/providers/local_libvirt/system_authority.py`:

1. `_require_matching_intent` compares the System subject only: `system_id`, `allocation_id`,
   `resource_id`, `authority_instance`, `root_identity`, `bootstrap_identity`. The stored
   `operation_digest` stays in the intent as a record of the generation that created it. The
   intent schema and the files that are already on hosts do not change.
2. The provision and teardown completion receipts are keyed by System and operation (their file
   path). Lookups stop comparing the stored `operation_digest` with the request. The receipt keeps
   the field.
3. `_execute_system_provision` resumes a retained domain by observation. When the matched intent's
   domain is present, it runs the existing retained-identity fence
   (`_verify_retained_provision_identity`), then returns the observed facts with
   `_complete_provision_facts(create=True)`. It does not check the deadline and does not call the
   provisioner again. Provisioning again would truncate the console log that the readiness probe
   reads (`prepare_console_readiness_window`), so a guest that is already running could never be
   seen as ready. The intent deadline check and the provisioner call stay for an absent domain.

The authority service is the owner of attempt identity. It authenticates each request's
generation and digest against the database and the journal chain before it calls the provider
(`resolve_current`, `_journal_state`). The provider only owns the private host state of one
System, so a per-generation check in the provider adds no protection.

Considered and not selected:

- Rewrite the intent with the current attempt, as the remote provider does. This needs a new
  intent schema, a migration of existing intent files, and replace-in-place writes. The local
  intent has no attempt-dependent fields, so the new record would not protect anything.
- Derive `operation_digest` per System instead of per generation. This changes migration 0149
  and the authority journal binding that the service and the external-boot authority use.

Generation 1 ending `retained-quarantine` on a fresh System is a separate cause. This change
does not explain it. The live check (criterion 4) shows whether a resumed generation reaches
`ready`. If it does not, the live evidence names the next fix inside the frozen surface, or the
quest stops and reports.

### Failure model

1. Actors and deployments: the external-boot authority host process, which runs
   `LocalAuthoritySystemProvider` for the authority's private libvirt daemon. The only caller is
   `AuthoritySystemService`, after authenticated peer, database, and journal checks.
2. Invariants and assets: one private intent per System. Its domain, overlay, and baseline are
   adopted only for the same subject (System, allocation, resource, authority instance, root,
   bootstrap key). A completion receipt never moves to another System or operation.
3. Accepted failure classes:
   - a later generation whose intent deadline has passed and whose domain is absent answers
     `provider-conflict` ("deadline expired"). Recovery is preactivation teardown. No domain or
     guest exists to resume, and the deadline bounds the host work of one System.
   - a retained domain that never reaches readiness stays `retained-quarantine`. Teardown owns
     it.
4. Covered elsewhere: attempt and generation authenticity — `AuthoritySystemService`
   (`resolve_current`, journal chain, migration 0149).

## Validation

- `focused-test`: a later generation (new `operation_digest`, same subject) observes the
  retained intent and does not raise. Red on `main`: `does not match the request`.
- `focused-test`: a later generation's `execute_system_provision`, after the intent deadline, with
  the owned domain present and ready, returns complete facts and does not call the provisioner.
  Red on `main`: `does not match the request`.
- `focused-test`: a different subject (other `allocation_id`) is still rejected with
  `does not match the request`.
- `focused-test`: a provision completion that one generation wrote replays for a later
  generation with the same `completed_at`. Red on `main`: `completion does not match`.
- `focused-test`: a teardown completion that one generation wrote replays for a later
  generation, on the no-intent path (observe) and the retained-intent path (execute). Red on
  `main`: `completion does not match`.
- Live check: criterion 4 on the POWER9 host. Pass evidence: the System is `ready` and the
  generation of its `provision-ready` receipt is recorded (1 means the resume path was not
  exercised live). After release and teardown, a `preactivation-absent` receipt exists and the
  private intent file is absent.
