# Authority install refusal reaches the job (#3535)

Decision record: [ADR-0814](../../adr/0814-a-guest-free-space-refusal-reaches-the-job-from-the-authority.md).

## Problem

A guest free-space refusal on the local external-boot authority path reaches the caller as a
generic `provider_conflict`. The job ends `infrastructure_failure` on its second attempt and the
sizes stay in the authority log. The fixed-worker path shows `install_failure` with the sizes.

## Scope

Change: the refusal crosses the authority as `install-failure` with three bounded integers, and the
job fails terminally on that attempt with `error_category = install_failure` and
`failure_context.install_refusal`.

Components:

| Unit | Change |
|------|--------|
| `guest_free_space.py` | `GuestFreeSpaceRefusal(CategorizedError)` for the shortfall raise only |
| `external_boot_authority/protocol.py` | `AuthorityInstallRefusalV1` |
| `external_boot_authority/service.py` | `install_failure` category, `install_refusal` argument and its pairing invariant; the `_answer_refusal` gate admits the category |
| `external_boot_authority/transport.py` | `_service_error` renders the frame at the four `AuthorityServiceError` sites |
| `local_libvirt/external_boot_authority.py` | `_apply` maps `GuestFreeSpaceRefusal` alone |
| `jobs/authority_sender.py` | decode the frame, raise terminal `install_failure` |
| `jobs/handlers/external_boot/runner.py` | copy the sizes into the failure context |
| `jobs/models.py` | closed `install_refusal` field, valid only with `install_failure` |
| `db/schema/0193_external_boot_install_refusal.sql` | field list, validity helper |

Ownership: a clean extension of existing owners; no move is needed. The refusal mapping stays in
`_apply`, the catch-all named in the issue evidence as `_commit`.

Not changed: other provider exceptions, free-form wire text, the remote adapter beyond the widened
`Literal`, the fixed-worker path. The second decoder in the sender
(`resolve_device_identity`) is untouched: `install-failure` stays out of `_PEER_REASONS`, so that
path keeps refusing it.

Data flow:

1. `GuestDebuginfoFile.stage` raises `GuestFreeSpaceRefusal` during `activate_modules`, at phase
   `pre-stop-intent`.
2. `_apply` validates the sizes into `AuthorityInstallRefusalV1`; invalid sizes log and become
   `provider_conflict`.
3. `execute_mutation` sends the error through `_answer_refusal`. A `conflict` observation wins.
   Otherwise the error is raised and the journal stays at `mutation-started`.
4. `_dispatch` writes the frame with `_service_error`.
5. The sender validates the frame and raises the terminal `CategorizedError`.
6. The runner's bound failure copies `details["install_refusal"]`; the commit validates and stores it.

## Failure model

1. **Actors and deployments**
   - Worker and local-libvirt authority processes on one host, in a rolling upgrade or matched.
   - The authority is trusted by the worker; the guest filesystem is not trusted by either.
   - An MCP reader of `jobs.get`.
2. **Invariants and assets at stake**
   - Only closed numeric facts cross the authority boundary (ADR-0584).
   - Every provider exception outside `GuestFreeSpaceRefusal` keeps its current wire category.
   - A refused request is never reported as a success, and a job never ends without a category.
   - The commit function refuses a malformed `install_refusal`.
   - ADR-0805: an observed third state is answered as a conflict.
3. **Accepted failure classes**
   - An older sender facing a newer authority sees `invalid-response` and retries to `max_attempts`.
     Bounded, and the stated upgrade order avoids it.
   - A newer runner on an unmigrated database fails the commit with 22023. The migration precedes
     the workers in the stated order.
   - The guest-supplied free figure is only as true as the guest's statistics. It is shown as a
     reported number and decides nothing past the refusal.
   - A refused activate with a `conflict` observation loses the sizes to the authority log.
4. **Covered elsewhere**
   - Settling the recovery point left at `pre-stop-intent`: existing recovery (#3130).
   - The size-less `_implausible` refusal: stays `provider_conflict`, ended by ADR-0714.
   - `failure_message` on authority-path jobs: not provided by this change or any other.
   - `runs.get`, `systems.get` and the failed-System admission message read `failure_message` and
     flat `failure_detail_*` keys, so they show the category and no sizes. The charter requires
     `jobs.get`.
   - Rollout order: stated in ADR-0814; no deploy tooling enforces it, as for ADR-0811.

## Threat model

1. **Boundary inventory.** Widened: the authority error frame, and the `fail` result into the commit
   function. Added: none.
2. **Actor model.** A guest can report arbitrary statistics. The authority and the worker hold
   credentials and are trusted to run kdive code; each still validates the other's frames.
3. **Control per boundary.** Guest to authority: `_free_bytes` and `AuthorityInstallRefusalV1`.
   Authority to worker: the canonical-bytes check plus the same model, with failure as
   `invalid-response`. Worker to database: `ExternalBootAuthorityFailureContext` and the SQL helper,
   with failure as 22023. Failures leak no value.
4. **Explicitly out of scope.** A compromised authority, which can already forge results.

## Success

- A `GuestFreeSpaceRefusal` raised during an activate ends the job on that attempt with
  `install_failure` and `failure_context.install_refusal` holding the three sizes.
- Every other exception from the local provider call keeps its current mapping: `provider_conflict`, including the
  size-less `install_failure`.
- The authority model, the sender and the commit function each refuse an out-of-range, non-integer,
  extra-key, missing-key or non-shortfall detail.
- `lifecycle_protocol_identity()` is unchanged.

## Validation

| Contract | Mode |
|----------|------|
| Wire model bounds | focused-test `tests/providers/external_boot_authority/test_install_refusal.py` |
| `_apply` mapping and fallback | focused-test `tests/providers/local_libvirt/test_external_boot_authority.py` |
| `_answer_refusal` gate | focused-test `tests/adversarial/test_external_boot_authority_journal.py` |
| Frame rendering | focused-test `tests/providers/external_boot_authority/test_transport.py` |
| Sender decode | focused-test `tests/jobs/test_authority_sender_install_refusal.py` |
| Bound failure and commit | focused-test `tests/jobs/handlers/external_boot/test_runner.py` |
| Commit function refusals | focused-test `tests/db/test_migration_0193_external_boot_install_refusal.py` |
| Migration window lists | focused-test the four `migrations[-K:]` tests |
| Lifecycle identity unchanged | task-test-not-applicable: compared once by hand; the value moves with any later schema change, so no fixed assertion can hold it |
| ADR text | task-test-not-applicable: human-read record, gated for shape by `just records` |
