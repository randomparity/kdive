# Plan — commit module-attempt intent before worker volume creation

Goal: provide the closed request carrier, server commit service, worker read-only verifier, and
verified authorization type required before #2170 can create either attempt volume.

Spec: `docs/workflow/specs/2026-09-05-module-attempt-obligation-receipt-design.md`.
Governing decisions: ADR-0588 and ADR-0605.

Expected implementation size: 220–340 changed lines (M), derived from one ~120-line contract and
service module, one small repository read method, and ~180 lines of focused unit/integration tests.

## Global constraints

- Python 3.14 with strict whole-tree `ty`; Ruff line length 100.
- No schema migration or privilege change. Migration 0126 remains authoritative.
- No `JobKind`, handler, libvirt call, discharge, or reaping changes.
- Guardrails: focused pytest, `just lint`, `just type`, `just test`, and
  `just ci > <file> 2>&1 < /dev/null`.
- Public GitHub bodies are file-backed and pass `just check-pr-body`.
- Native ppc64le live tests are excluded by campaign authority.

## File map

| Path | Action | Responsibility |
|---|---|---|
| `src/kdive/domain/remote_module_attempt_preparation.py` | create | strict canonical receipt/request models and verified authorization value |
| `src/kdive/db/remote_module_attempt_obligations.py` | modify | exact open-state read used by both roles |
| `src/kdive/services/remote_module_attempt_preparation.py` | create | server commit-before-return and worker verification services |
| `tests/domain/test_remote_module_attempt_preparation.py` | create | closed/canonical model and typed-authorization contracts |
| `tests/services/test_remote_module_attempt_preparation.py` | create | real-role commit, replay, lock lifetime, failure, and verification behavior |
| `tests/db/external_boot_authority_support.py` | consume unchanged | reusable isolated server/worker login fixture |

## Task 1 — implement the contract models with TDD

Verification: `focused-test` for the closed canonical receipt/request and typed authorization
contracts. Red command:
`uv run python -m pytest tests/domain/test_remote_module_attempt_preparation.py -q`; expected red
failure: import/collection fails because the new domain module does not exist. Green command: the
same command; expected green result: every strict-shape, canonical-byte, bound, and construction
case passes.

Create the domain test file first. Cover canonical request and receipt round trips; exact version,
UUID, nonce, and field shape; noncanonical ordering/trailing bytes; and the 4,096-byte decoder
bound. Assert `VerifiedModuleAttemptAuthorization` cannot be constructed without the module-private
witness and that its public accessor yields only the exact `ModuleAttempt`.

Run:

```text
uv run python -m pytest tests/domain/test_remote_module_attempt_preparation.py -q
```

Expect collection/import failure before implementation. Then create the domain values:

Create `remote_module_attempt_preparation.py` with one private canonical base, the two versioned
models, and the verified authorization value/factory. Reuse `ModuleAttempt` rather than defining a
second attempt tuple. Keep error messages structural and free of field values.

Run the green command. Prove the new tests bite by temporarily accepting one unknown field, observe
the unknown-field test fail, restore strict config, and rerun green.

## Task 2 — implement server and worker services with TDD

Verification: `focused-test` for the repository open-state query, commit-before-return service,
exact expected-attempt comparison, read-only worker verification, System-lock lifetime, redacted
failures, replay, and typed authorization handoff. Red command:
`uv run python -m pytest tests/services/test_remote_module_attempt_preparation.py -q`; expected red
failure: import/collection fails because the new service module and repository read method do not
exist. Green command: the same command; expected green result: every real-role, transaction,
failure, replay, and ordering case passes.

Create the service test file using the disposable migrated database. Re-export the existing
`authority_role_dsns` fixture from `tests.db.external_boot_authority_support` into this test module;
it creates isolated LOGIN roles with cleanup and is already safe for parallel test databases. Seed
the Resource→Run spine as administrator, then use separate `kdive_server` and `kdive_worker` pools.

Cover:

- the row is visible from a separate connection when the server service returns;
- injected insert/commit failure yields no request;
- replay returns identical canonical bytes;
- a discharged replay fails;
- worker verification succeeds only when the receipt, expected operation tuple, and open row match;
- missing, mismatched, discharged, and unreadable state return the same redacted failure;
- the worker role cannot insert/update the obligation; and
- a competing discharge holding the same System lock cannot run until the verification context's
  two-create probe exits; and
- only the verified value passes the production runtime gate and reaches a two-create probe once.

Run:

```text
uv run python -m pytest tests/services/test_remote_module_attempt_preparation.py -q
```

Expect failures for the missing repository/service interfaces before implementation.

Then add the exact open-state repository read:

Add `mutation_obligation_is_open(conn, attempt) -> bool`. One exact-key query returns true only for
an existing row with `mutation_discharged_at IS NULL`; missing and discharged rows return false.
It performs no write and is valid under both server and worker roles.

Implement the commit and verification services:

Create the service module. The server function owns `pool.connection()` and `conn.transaction()`,
opens idempotently, confirms open state, leaves both contexts, and only then constructs/returns the
request. The worker async context manager owns a read-only transaction, acquires
`advisory_xact_lock(..., LockScope.SYSTEM, expected_attempt.system_id)`, validates the typed request,
checks exact open state, maps missing/discharged/database failures to one redacted verification
error, and yields the verified authorization while retaining the lock. Add the production runtime
gate beside the authorization type; #2170's helper will reuse it.

Run both focused test files. Use a controlled fault that constructs/returns inside a transaction
test double before its commit marker; require the ordering assertion to fail, restore, and rerun
green.

## Task 3 — guardrails and review

Verification: `task-test-not-applicable` for the review and publication process itself because this
task changes no executable or structural product contract; Tasks 1–2 own all changed contracts and
their focused evidence. Repository guardrails and adversarial/security reviews remain mandatory.

Run, bare:

```text
just lint
just type
just test
just ci > .agent/ci-2251.log 2>&1 < /dev/null
```

Commit each behaviorally coherent fix separately. Run iterative adversarial review, the required
security pass for strict payload decoding and role-authority behavior, simplify without widening
scope, rerun changed guardrails, then publish a PR and drive hosted CI to green.

## Deferrals

None at plan time.
