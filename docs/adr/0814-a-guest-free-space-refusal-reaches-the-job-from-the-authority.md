# 0814 — A guest free-space refusal reaches the job from the authority

## Status

Accepted (2026-11-04)

## Context

The guest free-space check (#3125) refuses a vmlinux write as `install_failure` and names the sizes.
On the fixed-worker path that detail lands in the job's `failure_context`, so a `jobs.get` reader can
act on it. On the local external-boot authority path `LocalExternalBootAuthorityAdapter._apply` maps
every provider exception to `provider_conflict`, the authority error frame carries only
`{category, status}`, and the job ends `infrastructure_failure` with `authority_reason:
provider-conflict` after a pointless retry. The sizes reach only the authority log (#3535).

Three closed contracts stand between the refusal and the reader: the error frame, the runner's
`ExternalBootAuthorityFailureContext`, and the field list in
`commit_external_boot_authority_result`. [ADR-0584](0584-provider-host-authority-fences-external-boot-mutations.md)
keeps provider text off the wire and [ADR-0714](0714-repeated-provider-conflict-ends-the-external-boot-job.md)
ends a job on a repeated identical conflict.

## Decision

1. **One typed refusal.** `require_free_space_for_vmlinux` raises `GuestFreeSpaceRefusal`, a
   `CategorizedError` subclass in `install_failure`, when the sizes prove a shortfall. `_apply` maps
   that type alone to a new `AuthorityServiceError("install_failure", install_refusal=...)`. Every
   other provider exception keeps its existing mapping, including the size-less `install_failure`
   that `_implausible` raises for guest statistics it cannot trust: it stays `provider_conflict` and
   ADR-0714 ends a repeat. Sizes that do not fit the wire model also stay `provider_conflict`.
2. **One numeric frame.** `AuthorityInstallRefusalV1` is closed and holds `vmlinux_bytes`,
   `free_bytes` and `headroom_bytes`: strict integers in 0..2^63-1 with free below the sum of the
   other two. The only error frame with a third key is
   `{category: "install-failure", install_refusal: {...}, status: "error"}`. No text crosses, so the
   directory and target stay in the authority log. An `AuthorityServiceError` cannot be built with
   the category and no sizes, or with sizes and another category.
3. **The sender validates and ends the job.** The sender accepts that frame through the same model
   and raises `CategorizedError(install_failure, terminal=True)`. A bare `install-failure` category,
   or the sizes on any other category, is `invalid-response`. `terminal` is required: the commit
   requeues on the flag and does not consult `RETRYABLE_BY_CATEGORY`.
4. **The job row holds the sizes.** `failure_context` gains the closed `install_refusal` object, valid
   only with `error_category = install_failure`. Migration 0193 adds it to the commit function's field
   list and validates it through `external_boot_install_refusal_is_valid`. `jobs.get` shows it
   through the existing `failure_context` merge.
5. **The ADR-0805 observation still runs.** `execute_mutation` sends an `install_failure` through
   `_answer_refusal` as it does a `provider_conflict`. A refused activate whose fresh observation is
   a third state answers that conflict and the sizes stay in the authority log.

## Consequences

- **Upgrade order:** migrate, then workers, then authorities. An older sender rejects the frame as
  `invalid-response`, which retries as `infrastructure_failure` until `max_attempts` and hides the
  sizes, so the failure is bounded. A newer sender accepts an older authority unchanged. A newer
  runner cannot commit `install_refusal` against an unmigrated database: the commit refuses with
  SQLSTATE 22023 and the job stays `running` until its lease lapses.
- **Job outcome:** a refused job ends on its first attempt with `install_failure`, where it took two
  attempts to end as `infrastructure_failure`. The later state is otherwise the same: the job and
  run fail, the authority retires, the activation is untouched, and the journal stays at
  `mutation-started` with the recovery point at `pre-stop-intent`.
- **Lifecycle identity:** the lifecycle request and response schemas do not include these models, so
  the identity does not move. Verified by comparing `lifecycle_protocol_identity()` before and after.
- **Remote authority:** the shared `Literal` widens. The remote adapter never raises the category.
- **Message text:** only the numbers reach the job, in `install_refusal`. The worker log carries a
  rendered message, and `failure_message` stays absent on authority-path jobs.

## Considered & rejected

- **Stop the sizes at the worker log.** judgment: the `jobs.get` reader, who has to resize the disk,
  would still have no figures. The operator chose job-level on 2026-11-04.
- **Map every `CategorizedError(install_failure)`.** verified: `_implausible` in
  `guest_free_space.py` raises the category with no sizes at commit `1e64adede`, so the frame would
  need optional fields and the wire would stop proving a shortfall.
- **Add a free-form `detail` string to the frame.** judgment: it reopens the text path ADR-0584 closed
  and cannot be validated.
- **Detect the refusal by sniffing `details` keys.** judgment: a closed type states the mapping; a key
  check makes any future raise with the same keys a wire contract.
- **Leave `terminal` false and rely on the category.** verified: `v_terminal` in
  `commit_external_boot_authority_result` reads only the flag, `max_attempts` and the ADR-0714 repeat
  (`0170_repeated_provider_conflict_terminal.sql`).
