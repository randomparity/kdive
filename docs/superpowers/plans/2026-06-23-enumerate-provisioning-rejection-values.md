# Plan — Enumerate valid rootfs/catalog values in provisioning rejections (#731)

- **Spec:** [docs/specs/2026-06-23-enumerate-provisioning-rejection-values.md](../../specs/2026-06-23-enumerate-provisioning-rejection-values.md)
- **ADR:** [ADR-0224](../../adr/0224-enumerate-provisioning-rejection-values.md)
- **Issue:** #731
- **Branch:** `feat/enumerate-provision-rejects-731` (external worktree, `--rebase` merge)

## Conventions for every task

- Python 3.14, `uv`. TDD: write the failing test, confirm it fails for the expected reason,
  then the minimal implementation.
- Guardrails before each commit (run from the worktree root): `just lint` (ruff check +
  format check), `just type` (ty, **whole tree**), and the focused test file. Run the full
  `just ci` once before push (step 4).
- 100-char lines, Google-style docstrings on non-trivial public APIs, absolute imports only.
- Conventional-commit subjects ≤72 chars, ending with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- No new dependencies, no MCP-surface/port/schema/migration change.
- The enumerated values are operator-declared names and operator-configured roots only — never
  echo caller input or a secret-shaped string (no-leak, ADR-0123).

Tasks are ordered: Task 1 is the enabling serialization change (everything else depends on it);
Tasks 2 and 3 are independent of each other once Task 1 lands; Task 4 is the end-to-end seam.

---

## Task 1 — Reserve bounded scalar-list enumeration keys in `safe_error_details`

**Where it fits:** Spec R3. The shared filter `safe_error_details`
(`src/kdive/serialization.py:91-108`) drops every non-scalar detail except a reserved `errors`
list. Without this change, the `available`/`accepted_values` lists added in Tasks 2-3 are
silently dropped before the wire. This is the enabling change.

**Files touched:** `src/kdive/serialization.py`, `tests/test_serialization.py` (or the existing
test module for `safe_error_details` — locate with `rg -l safe_error_details tests/`).

**Change:**
- Add a module constant naming the reserved enumeration keys, e.g.
  `_ENUMERATION_KEYS = frozenset({"accepted_values", "available"})`.
- In `safe_error_details`, before the scalar reduction, add a branch: when `key` is in
  `_ENUMERATION_KEYS` and `value` is a `list`, preserve a bounded list of its scalar elements —
  `[s for item in value[:_MAX_ERROR_ENTRIES] if (s := _scalar_or_none(item)) is not None]`.
  Reuse the existing `_MAX_ERROR_ENTRIES = 20` cap. Mirror the structure of the existing
  `errors`-list branch (lines 101-104).
- Every other key path is unchanged: a non-reserved key with a list value still falls through to
  `_scalar_or_none`, which returns `None`, so it is dropped.

**TDD — failing tests first** (in the `safe_error_details` test module):
1. `details={"available": ["b/y", "a/x"]}` → result `{"available": ["b/y", "a/x"]}` (order
   preserved as given; sorting is the caller's job, see Tasks 2-3). Confirm it fails today
   (the list is dropped).
2. `details={"accepted_values": ["/r1", "/r2"]}` → preserved.
3. Non-scalar element dropped: `details={"available": ["ok", {"x": 1}, 5]}` →
   `{"available": ["ok", 5]}`.
4. Cap: `details={"available": [str(i) for i in range(30)]}` → length 20.
5. Empty list preserved: `details={"available": []}` → `{"available": []}`.
6. Non-reserved key with a list is still dropped: `details={"supported": ["a", "b"]}` → `{}`
   (regression guard for R3's "unchanged behaviour").
7. A reserved key with a **non-list scalar** value is unaffected (falls through to the scalar
   branch): `details={"available": "x"}` → `{"available": "x"}` (defensive; the enum branch
   only triggers on a list).

**Acceptance:** the seven cases pass; `just type` clean (the new branch must keep the
`dict[str, JsonValue]` return type — elements are `JsonValue`).

**Rollback:** revert the single function change; the new tests are self-contained.

---

## Task 2 — Surface `available` declared catalog names on the unknown-catalog rejection

**Where it fits:** Spec R1. `validate_rootfs_reference`
(`src/kdive/profiles/provisioning.py:401-438`) raises `unknown rootfs catalog name` and discards
the inventory doc it loaded one function below in `_catalog_name_declared`.

**Files touched:** `src/kdive/profiles/provisioning.py`,
`tests/providers/local_libvirt/test_rootfs_resolve.py`.

**Change:**
- Refactor so the loaded inventory doc is available at the raise site. `_catalog_name_declared`
  currently both loads the doc and decides membership. Either (a) have it return the doc (or
  `None`) and move the membership check into `validate_rootfs_reference`, or (b) add a small
  helper `_declared_catalog_entries() -> list[str] | None` that loads the doc and returns the
  sorted `"provider/name"` strings (or `None` when the file is absent). Prefer (b): it keeps
  `_catalog_name_declared` intact (other call sites unaffected) and isolates the enumeration.
- In the raise, when the doc is present, attach
  `details={"provider": …, "name": …, "available": _declared_catalog_entries()}`.
  Build entries as `sorted(f"{img.provider}/{img.name}" for img in doc.image)`. When the file is
  absent the rejection never fires (the validator returns early), so `available` is only ever
  attached alongside a real rejection.
- Keep the existing `provider`/`name` scalar details (they already survive the filter).

**TDD — failing tests first** (extend `test_rootfs_resolve.py`, which already has
`_DECLARED_SYSTEMS_TOML` and the `KDIVE_SYSTEMS_TOML` monkeypatch fixture):
1. `test_..._rejects_undeclared_catalog_name_enumerates_available`: with the declared toml,
   reject `name="no-such"` and assert `exc.details["available"] == ["local-libvirt/known"]`
   (sorted `provider/name`). Confirm it fails today (`available` absent).
2. Multi-image inventory (declare two `[[image]]`): assert `available` is sorted and contains
   both `provider/name` strings.
3. No-leak: assert `available` contains only the declared `provider/name` strings — no caller
   input (`"no-such"` must not appear), no path, no secret-shaped token.
4. The already-passing `accepts_declared` / `defers_to_db_when_no_systems_toml` tests still pass
   (the absent-file path attaches nothing).

**Acceptance:** the new assertions pass; existing `test_rootfs_resolve.py` tests stay green;
`just type` clean.

**Rollback:** revert the helper + the `details` enrichment; tests are additive.

---

## Task 3 — Surface `accepted_values` allowed roots on the outside-roots rejection

**Where it fits:** Spec R2. `validate_local_component_path`
(`src/kdive/components/local_paths.py:13-34`) raises `… is outside provider allowed roots` and
discards the `allowed_roots` parameter (and the already-resolved `roots` list at line 28).

**Files touched:** `src/kdive/components/local_paths.py`,
`tests/provider_components/test_local_paths.py`.

**Change:**
- The outside-roots branch (line 29-30) already computed `roots` (the resolved root paths,
  line 28). Attach them: raise the `CategorizedError` for that branch with
  `details={"accepted_values": sorted(str(root) for root in roots)}`.
- `_config_error` is a one-arg helper today. Either extend it to accept optional `details`, or
  build the `CategorizedError` inline for this one branch. Prefer extending `_config_error`
  with a keyword-only `details: dict[str, object] | None = None` so the call site stays one
  line and every other call (which passes no details) is unchanged.
- Only the outside-roots branch attaches `accepted_values`. The other branches (not absolute,
  does not exist, not a regular file, not readable, sha mismatch) name no finite valid set and
  stay bare — spec "out of scope".

**TDD — failing tests first** (extend `test_local_paths.py`, which already constructs
`allowed_roots` and asserts on the `outside provider allowed roots` message):
1. `test_..._outside_roots_enumerates_accepted_values`: call with a path outside the roots and
   `allowed_roots=[rootA, rootB]`; assert
   `exc.details["accepted_values"] == sorted([str(rootA.resolve()), str(rootB.resolve())])`.
   Confirm it fails today (`details` empty). Match the resolution the code applies
   (`root.resolve(strict=False)`), so the test asserts the same canonical strings the code
   emits.
2. No-leak: assert `accepted_values` contains only the configured roots — the caller-submitted
   `path` (the bad value) must not appear in `accepted_values`.
3. Other rejections unchanged: the "does not exist" / "not absolute" tests still see empty
   `details` (no enumeration), proving the attach is scoped to the outside-roots branch.

**Acceptance:** new assertions pass; existing `test_local_paths.py` tests stay green;
`just type` clean.

**Rollback:** revert the `_config_error` signature change + the one enriched raise.

---

## Task 4 — End-to-end: enumeration reaches `systems.provision` response `data`

**Where it fits:** Spec acceptance test #4. Proves the Task-1 filter change actually carries the
Task-2/3 lists through the admission envelope (`safe_error_details` →
`AdmissionFailure.failure_details` → `_admission_failure_data` → response `data`) to the
`systems.provision` wire surface. This is the assertion that the bug is fixed on the real path,
not just in unit isolation.

**Files touched:** test-only. Locate the existing admission/provision test module
(`rg -l "admission|_failure_from_error|safe_error_details" tests/services/systems tests/mcp`)
and add a focused test at the tightest boundary the project already uses for this — preferably
driving `_failure_from_error` (admission.py:166) with a `CategorizedError` carrying an
`available`/`accepted_values` list and asserting `failure_details` contains the list, then
through `_admission_failure_data` (`provision.py:65`) asserting it lands in `data`. If an
existing test already drives a full `systems.provision` admission with a rootfs rejection,
extend that instead of adding a parallel harness.

**TDD — failing test first:**
1. Construct a `CategorizedError(category=CONFIGURATION_ERROR,
   details={"name": "x", "available": ["local-libvirt/known"]})`, pass it through
   `_failure_from_error`, and assert `result.failure_details["available"] ==
   ["local-libvirt/known"]`. Before Task 1 this fails (list dropped); after Task 1 it passes —
   so this test is the integration guard that Task 1's reservation is wired to the admission
   path, not just the `failure_from_error` path.
2. Assert the same list survives `_admission_failure_data` into `data`.

**Acceptance:** the end-to-end assertion passes; no new test harness duplicated; `just ci`
green.

**Rollback:** test-only; revert the added test.

---

## Final verification (step 4/5 of work-issue)

- `just ci` (full gate) green from the worktree root before first push. If `check-mermaid`
  fails locally for a missing-node-module reason (the worktree's `.github/scripts` deps aren't
  installed), note it in the PR body and rely on CI — the new docs contain no mermaid.
- Adversarial branch review (`/challenge --base main`) + `security-review` of the diff;
  address findings.
- PR body: plain factual description of the two enriched rejections + the filter reservation;
  `Closes #731`.

## What could still go wrong (verification focus for the implementer)

- `_materialize_rootfs_base` is confirmed to surface the `validate_local_component_path`
  `CategorizedError` without re-wrapping (`materialize.py:62-63` calls it directly; the only
  `try/except CategorizedError` in `provision` re-raises after overlay cleanup,
  `provisioning.py:192-196`). If Task 3's detail is somehow lost on the rootfs lane, Task 4's
  test catches it.
- `doc.image` iteration order is not guaranteed stable across loads — the `sorted(...)` in
  Task 2 is load-bearing for R1's "stable wire order"; the test must assert the sorted result,
  not the insertion order.
