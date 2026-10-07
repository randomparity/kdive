# External-boot debuginfo evidence and plan member — implementation plan (#3129)

Goal: build evidence and the external-boot plan describe the uploaded `vmlinux`, without
changing the identity of any stored plan, and the reservation counts its bytes.

Architecture: completion hashes the `vmlinux` into `external-boot-evidence-v1`; plan
construction copies it into an optional `ExternalBootPlan.debuginfo` member that `model_dump`
omits when absent; one shared function owns the materialization reservation. Spec:
[design](../specs/2026-10-06-external-boot-debuginfo-plan-design.md). Decision: ADR-0724.

Tech stack: Python 3.14, pydantic 2.13 (`Field(exclude_if=...)` is available), pytest.

Expected implementation size: 160–230 changed lines (M) — four source files, two caller
migrations, five test files, from the task list below.

## Global Constraints

- Ruff line length 100; `ty` strict. Guardrails: `just lint`, `just type`, `just test-changed`,
  `just records`, `just docs-check`, `just env-docs-check`, `just config-docs-check`,
  `just adr-status-check` (after `git fetch origin main`).
- Code cites #3129, not ADR-0724, while ADR-0724 is Proposed (`adr-status-check`).
- The 8 GiB bound is `8_589_934_592` bytes in both `validation.py` and `ports/external_boot.py`.
- Doc style: plain prose; no "critical", "robust", "comprehensive", "elegant".

## Task 1 — Plan member with canonical omission

Files: `src/kdive/providers/ports/external_boot.py`; test `tests/providers/ports/test_external_boot.py`.

Interfaces: provides `DebuginfoSource(ArtifactSource)` with `size_bytes: int` and
`ExternalBootPlan.debuginfo: DebuginfoSource | None`.

Verification:
- Contract: absent member keeps canonical bytes. Mode: focused-test —
  `test_plan_matches_adr_golden_vector_and_identity` stays green unchanged, plus new
  `test_plan_rejects_an_explicit_null_debuginfo_as_non_canonical` (red: field unknown).
- Contract: present member round-trips and changes identity. Mode: focused-test —
  `test_plan_with_debuginfo_round_trips_canonically` (red: `extra_forbidden`).
- Command: `uv run python -m pytest tests/providers/ports/test_external_boot.py -q` → all pass.

Steps:
1. Write both tests. The round-trip test builds `_plan_data() | {"debuginfo": {"key": "b/vmlinux",
   "version": "v1", "sha256": ZERO_DIGEST, "size_bytes": 4096}}`, asserts
   `from_canonical_json(to_canonical_json()) == plan`, `b'"debuginfo"' in` the bytes, and the
   identity differs from the golden one. The null test asserts `from_canonical_json` of the golden
   bytes with `"debuginfo":null` inserted in sorted position raises `ValueError` matching
   `not canonical`. Run: red.
2. Add `_DEBUGINFO_MAX_BYTES = 8_589_934_592` and
   ```python
   class DebuginfoSource(ArtifactSource):
       size_bytes: Annotated[int, Field(ge=1, le=_DEBUGINFO_MAX_BYTES)]
   ```
   and on `ExternalBootPlan`, after `root`:
   ```python
   # Omitted when absent so plans persisted before #3129 keep their canonical identity.
   debuginfo: DebuginfoSource | None = Field(None, exclude_if=lambda value: value is None)
   ```
3. Run: green. Commit `feat(external-boot): add optional debuginfo plan member`.

## Task 2 — SQL identity agrees for a plan with the member

Files: test `tests/db/test_external_boot_authority_preparation_migration.py`.

Verification: Mode: focused-test — new `test_sql_plan_identity_matches_python_with_and_without_debuginfo`
computes, over `Jsonb(plan.model_dump(mode="json", by_alias=True))`,
`'sha256:' || encode(sha256(convert_to('kdive-external-boot-plan-v1','UTF8') || decode('00','hex')
|| convert_to(public.canonical_external_boot_authority_json(%s::jsonb),'UTF8')),'hex')` on
`psycopg.connect(migrated_url)` and asserts it equals `plan.identity` for
`external_boot_plan(uuid4(), uuid4())` and for that plan with `model_copy(update={"debuginfo":
DebuginfoSource(...)})`. Red check: temporarily drop `exclude_if` → the no-member case fails.
Command: `uv run python -m pytest tests/db/test_external_boot_authority_preparation_migration.py -q -k sql_plan_identity` → pass (needs Docker).

Steps: write the test, run red by the controlled fault, revert it, run green, commit
`test(external-boot): prove SQL plan identity with a debuginfo member`.

## Task 3 — Evidence records the vmlinux

Files: `src/kdive/build_artifacts/validation.py`; test
`tests/providers/local_libvirt/test_validate_external_artifacts.py`.

Verification:
- Contract: evidence carries `debuginfo` with digest and size. Mode: focused-test — new
  `test_external_boot_evidence_records_the_uploaded_vmlinux` calls `_external_boot_evidence` with
  `heads` and `keys` that include `vmlinux` (an ELF blob from `_elf_with_build_id`, build id
  `deadbeef` to match the kernel); asserts `evidence["debuginfo"] == {"sha256": "sha256:" +
  sha256(blob), "size_bytes": len(blob)}`. Red: `KeyError`.
- Contract: no `vmlinux` → no key. Mode: focused-test —
  `test_external_boot_evidence_matches_pre_stream_guard_baseline` stays unchanged and green.
- Contract: over-bound size fails. Mode: focused-test — new
  `test_external_boot_evidence_rejects_an_oversize_vmlinux` monkeypatches
  `validation._EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES` to `len(blob) - 1` and expects `CategorizedError`
  matching `vmlinux exceeds the external-boot byte limit`.
- Command: `uv run python -m pytest tests/providers/local_libvirt/test_validate_external_artifacts.py -q`.

Steps:
1. Write the tests; run red.
2. Add `_EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES = 8 * 1024 * 1024 * 1024`; add
   `max_bytes=_EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES` to the `vmlinux` `FormatContract`. In
   `_external_boot_evidence`, before `bundle_digest.drain()`:
   ```python
   debuginfo_head = heads.get("vmlinux")
   if debuginfo_head is not None:
       if debuginfo_head.size_bytes > _EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES:
           raise _build_failure(
               "vmlinux exceeds the external-boot byte limit; strip unused debug sections",
               name="vmlinux",
               max_bytes=_EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES,
           )
   ```
   and after building the return dict, `if debuginfo_head is not None: evidence["debuginfo"] =
   {"sha256": _digest_object(store, keys["vmlinux"], debuginfo_head.size_bytes), "size_bytes":
   debuginfo_head.size_bytes}`.
3. Run green; run `just docs-check` (the contract may render into generated docs; regenerate
   with the recipe it names if it fails). Commit `feat(builds): record vmlinux facts in
   external-boot evidence`.

## Task 4 — Plan construction copies the evidence

Files: `src/kdive/services/external_boot/plan.py`; test `tests/services/external_boot/test_plan.py`.

Verification:
- Contract: evidence `debuginfo` → plan member. Mode: focused-test — new
  `test_construct_plan_carries_the_uploaded_vmlinux` adds `"debuginfo": {"sha256": _SHA,
  "size_bytes": 4096}` to the evidence, `"vmlinux": {"version_id": "vmlinux-v1"}` to artifacts,
  `"debuginfo_ref": "builds/vmlinux"` to `build_result`; asserts `plan.debuginfo ==
  DebuginfoSource(key="builds/vmlinux", version="vmlinux-v1", sha256=_SHA, size_bytes=4096)`.
- Contract: no evidence key → no member, even with a `debuginfo_ref` (pre-change build). Mode:
  focused-test — `test_construct_plan_omits_debuginfo_for_evidence_without_it`.
- Contract: incomplete inputs fail. Mode: focused-test —
  `test_construct_plan_rejects_debuginfo_evidence_without_its_object` (evidence key, no
  `debuginfo_ref`) expects `details["reason"] == "external_boot_debuginfo_incomplete"`.
- Command: `uv run python -m pytest tests/services/external_boot/test_plan.py -q`.

Steps: write tests (red), then after the initrd block in `construct_external_boot_plan`:
```python
debuginfo_evidence = evidence.get("debuginfo")
if debuginfo_evidence is not None:
    vmlinux = artifacts.get("vmlinux")
    debuginfo_ref = result.get("debuginfo_ref")
    if (
        not isinstance(debuginfo_evidence, dict)
        or vmlinux is None
        or not isinstance(debuginfo_ref, str)
    ):
        raise _invalid("external_boot_debuginfo_incomplete")
    data["debuginfo"] = {
        "key": debuginfo_ref,
        "version": vmlinux.get("version_id"),
        "sha256": debuginfo_evidence.get("sha256"),
        "size_bytes": debuginfo_evidence.get("size_bytes"),
    }
```
Run green; commit `feat(external-boot): carry the vmlinux into the plan`.

## Task 5 — One reservation function counts the vmlinux

Files: `src/kdive/providers/shared/external_boot_bounds.py`,
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` (`_RealLocalExternalBootOperation.materialize`),
`src/kdive/providers/remote_libvirt/external_boot_materialization.py` (`materialize`);
test `tests/providers/shared/test_external_boot_bounds.py`.

Interfaces: provides `materialization_reservation_bytes(plan: ExternalBootPlan) -> int`;
`source_byte_limit` accepts `DebuginfoSource`.

Verification:
- Contract: reservation grows by exactly the member size. Mode: focused-test —
  `test_reservation_counts_the_debuginfo_size` compares `external_boot_plan(...)` with and
  without a 4096-byte member: difference is 4096. Red: `ImportError`.
- Contract: `source_byte_limit(DebuginfoSource)` is its `size_bytes`. Mode: focused-test —
  `test_source_byte_limit_uses_the_closed_debuginfo_size` (red: `TypeError`).
- Contract: the bound matches validation's. Mode: focused-test —
  `test_debuginfo_bound_matches_completion_bound` asserts `DebuginfoSource` rejects
  `validation._EXTERNAL_BOOT_DEBUGINFO_MAX_BYTES + 1` and accepts the bound.
- Contract: the local caller accepts equality and refuses one byte over, now with the member.
  Mode: focused-test — `test_real_materializer_capacity_accepts_equality_and_refuses_one_over`
  in `tests/providers/local_libvirt/test_external_boot.py` computes `reservation =
  materialization_reservation_bytes(plan) + _MAX_PROJECTION_BYTES + _MAX_RECOVERY_METADATA_BYTES`
  for `_plan()` with a 4096-byte `debuginfo` member (`model_copy(update=...)`); its inline sum
  goes away. Red: the old local formula refuses at equality (it omits 4096 bytes).
- Contract: the remote caller still refuses an over-capacity plan. Mode: focused-test —
  `test_concrete_remote_materializer_rejects_capacity_and_foreign_binding` stays green.
- Command: `uv run python -m pytest tests/providers/shared/test_external_boot_bounds.py tests/providers/local_libvirt tests/providers/remote_libvirt -q`.

Steps:
1. Write the tests; red.
2. In bounds:
   ```python
   def materialization_reservation_bytes(plan: ExternalBootPlan) -> int:
       """Return the artifact bytes one activation may materialize (ADR-0602, #3129)."""
       initrd = 0 if plan.initrd is None else plan.initrd.size_bytes
       debuginfo = 0 if plan.debuginfo is None else plan.debuginfo.size_bytes
       return (
           plan.bundle.decoded_kernel_size_bytes
           + initrd
           + debuginfo
           + plan.module_obligation.uncompressed_bytes
           + plan.module_obligation.member_count * 1024
           + MAX_MODULE_ARCHIVE_BYTES * 2
           + source_byte_limit(plan.bundle)
       )
   ```
   and `if isinstance(source, (InitrdSource, DebuginfoSource)): return source.size_bytes`.
3. Local: `reservation = materialization_reservation_bytes(plan) + _MAX_PROJECTION_BYTES +
   _MAX_RECOVERY_METADATA_BYTES`. Remote: `reservation = materialization_reservation_bytes(plan) +
   _TEMPORARY_METADATA_BYTES`. Remove the now-unused `initrd_bytes` locals and imports.
4. Run green; commit `refactor(external-boot): share the materialization reservation`.

## Final

Run every guardrail in Global Constraints, then `just test-linux HEAD`.
