# External-boot evidence and plan carry the uploaded vmlinux (#3129)

Decision: [ADR-0724](../../adr/0724-external-boot-plan-carries-an-optional-debuginfo-member.md).
Issue: #3129, piece 1 of 3 of #3123. Builds on ADR-0723 item 5.

## Problem

An external-boot authority fetches only the exact objects that the closed plan names. The
build evidence and the plan do not name the Run's uploaded `vmlinux`, so no authority can stage
it (#3130, #3131). The plan identity is persisted, so the change must keep every stored plan's
canonical bytes unchanged.

## Scope

1. `src/kdive/build_artifacts/validation.py`: `_external_boot_evidence` adds
   `"debuginfo": {"sha256", "size_bytes"}` when `heads` has `vmlinux`, and no key otherwise.
   The digest reuses `_digest_object`. A `vmlinux` over `_EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES`
   (8 GiB) fails completion with `_build_failure`, and the `vmlinux` `ArtifactContract` states
   that `max_bytes`.
2. `src/kdive/providers/ports/external_boot.py`: new `DebuginfoSource(ArtifactSource)` with
   `size_bytes` from 1 to 8 GiB. `ExternalBootPlan.debuginfo: DebuginfoSource | None`, default
   `None`, excluded from `model_dump` when `None` (`Field(exclude_if=...)`). This one rule covers
   canonical bytes, job payloads and the SQL identity, which all read `model_dump`.
3. `src/kdive/services/external_boot/plan.py`: when the evidence has a `debuginfo` key, the plan
   gets `{key: build_result.debuginfo_ref, version: artifacts.vmlinux.version_id, sha256,
   size_bytes}`. A `debuginfo` key without a string `debuginfo_ref` or a `vmlinux` artifact
   fails with reason `external_boot_debuginfo_incomplete`. No key gives no member.
4. `src/kdive/providers/shared/external_boot_bounds.py`: `source_byte_limit` returns
   `size_bytes` for `DebuginfoSource`. New `materialization_reservation_bytes(plan)` returns the
   artifact part of the reservation, now plus the `debuginfo` size. The local
   (`providers/local_libvirt/lifecycle/boot/external_boot.py`) and remote
   (`providers/remote_libvirt/external_boot_materialization.py`) reservations call it and add
   their own metadata overhead. Their inline sums are removed. This is an ownership change: the
   two copies of one policy become one function (criterion 4).
5. No authority reads the member (criterion 5). Staging is #3130 and #3131.

Out of scope, with owners: guest free-space check (#3125), remote-libvirt legacy delivery
(#3124), catalog `drgn_version` drift (#3122), local staging and live proof (#3130), remote
stage implementation (#3131).

## Failure model

1. **Actors and deployments:** an agent or operator that completes an uploaded build; the
   worker that builds a plan at install; the local-libvirt and remote-libvirt authority hosts.
2. **Invariants and assets:** the identity of every plan stored before this change; the SQL
   identity check in migration 0135; the published upload contract.
3. **Accepted failure classes:**
   - A Run completed before this change gets no member even with a `vmlinux`. Bounded: the
     ADR-0723 probe reports `debuginfo_unloadable`; a new Run fixes it.
   - A pre-change process rejects a plan with the member during a mixed-version rollout.
     Bounded: the boot fails closed with a validation error.
   - Until #3130 and #3131 land, authorities ignore the member. The probe reports it.
4. **Covered elsewhere:** guest disk space for the file (#3125); recovery of the staged file
   (#3130, #3131).

## Success

- A plan without the member has the same canonical bytes and identity as before; the existing
  golden vector passes unchanged.
- A plan with the member round-trips through `from_canonical_json`, and the SQL identity
  expression in migration 0135 gives the same identity as `ExternalBootPlan.identity`.
- Completion with a `vmlinux` records its digest and size; without one, the evidence has no
  `debuginfo` key and matches the existing baseline.
- The reservation grows by exactly the member's `size_bytes`.

## Validation

Unit tests for each item above, plus one database test for the SQL identity. Details per task
are in the [plan](../plans/2026-10-06-external-boot-debuginfo-plan.md).
