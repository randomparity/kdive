# Implementation plan — external-boot idempotency and bounded failures

Issue [#2202](https://github.com/randomparity/kdive/issues/2202). Spec:
[2026-09-04-external-boot-idempotency-design.md](../specs/2026-09-04-external-boot-idempotency-design.md).
Decisions: ADR-0583, ADR-0584, ADR-0593, and ADR-0595.

## Constraints and durable state

- Base branch `main`; branch `feat/external-boot-idempotency-2202`.
- Host `x86_64`; targets `x86_64` and `ppc64le`; native ppc64le live proof excluded.
- Claim/scope token `q2202-ce529ae2`; revised scope comment records the operator-authorized
  server-preparation and provider-port/adapter expansion.
- Migration `0128`; ADR `0595`.
- Review depth `iterating`; classification `non-trivial`.
- Guardrails: focused pytest, `just lint`, `just type`, `just test-changed`, then
  `just ci > PRIVATE_FILE 2>&1 < /dev/null`.
- No MCP tool, agent-facing contract, new dependency, or reconciler lane.

## Task 1 — Closed receipts and failure contract

Add failing model tests for preparation requests/observations, exact owner and operation binding,
the three CAS reason/action/terminal combinations, and the authority executor protocol. Implement
the closed models in `providers/ports/external_boot.py`, widen the injected handler authority seam,
and add migration 0128 with exact replay for deadline and attempt results. Update migration fixtures.

Verification: `uv run python -m pytest tests/providers/ports/test_external_boot.py
tests/jobs/test_external_boot_authority_models.py
tests/db/test_migration_0128_external_boot_reentry_failures.py -q`.

Commit: `feat(external-boot): define durable reentry receipts`.

## Task 2 — Server preparation re-entry

Add a server preparation service that reads the activation under the System lock, observes the
provider receipt, executes only an absent phase, and consumes every repository CAS result. Add the
fault-inject preparation receipt and interruption controls. Prove loss after materialize and prepare
returns converges without a second provider mutation.

Verification: `uv run python -m pytest tests/services/external_boot/test_preparation.py
tests/providers/fault_inject/test_external_boot.py -q`.

Commit: `feat(external-boot): resume server preparation from receipts`.

## Task 3 — Worker authority execution and lifecycle re-entry

Route worker operations through the authority executor rather than direct runtime mutation. Observe
before redo, require operation-specific source/target/absence outcomes, reuse the activation
deadline, create/reuse/finish recovery attempts, and classify every losing CAS. Inject worker loss
after activate, release, recover, and cleanup provider returns. Assert row/ledger equality and one
provider mutation.

Verification: `uv run python -m pytest tests/jobs/handlers/external_boot/test_reentry.py
tests/jobs/handlers/external_boot/test_lifecycle.py -q`.

Commit: `feat(external-boot): make worker lifecycle reentrant`.

## Task 4 — Local adapter contract

Add local-libvirt atomic preparation receipts and bind them to the existing recovery metadata.
Exercise authority-journal replay and cleanup absence. Run focused local-libvirt contract tests;
run the applicable live local-libvirt proof on this host after unit gates are green. Remote-libvirt
implementation stays with #2200; this change supplies only the provider-neutral contract it may
adopt.

Verification: `uv run python -m pytest tests/providers/local_libvirt/test_external_boot.py
tests/providers/local_libvirt/test_external_boot_authority.py -q`.

Commit: `feat(providers): persist external boot operation receipts`.

## Task 5 — Bounded mapping, debt closure, and gates

Enumerate fault-inject failures and pin the committable category tuple. Assert serialized failures
contain no injected raw text or provider identifiers. Resolve debt record 0005 with exact migration,
handler, and test evidence.

Run `just test-changed`, `just lint`, and `just type`. Then run
`just ci > PRIVATE_FILE 2>&1 < /dev/null` and inspect the private log on failure. Commit the debt
record separately after executable evidence is green.

## Design-review accounting

The prior four findings are accepted by this revised design:

1. Materialize/prepare now have real server-side post-port/pre-commit replay through preparation
   receipts.
2. Cleanup success is observable through the authority adapter's accounted-absence state and
   journal receipt.
3. Worker recovery uses full authority observations rather than running-kernel identity alone.
4. CAS reasons now define terminal/requeue and deadline behavior explicitly.

The scope-audit finding about remote-libvirt was accepted as a cut. Issue #2200 remains the owner
of remote adoption. No rejected findings are carried from the design review.
