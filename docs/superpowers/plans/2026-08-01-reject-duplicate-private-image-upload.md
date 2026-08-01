# Reject Duplicate Private Image Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject an `images.upload` whose private project/provider/name is already registered before any published object write, while preserving the existing concurrent-attempt integrity guarantees.

**Architecture:** Add a registered-identity query to the existing PROJECT-locked reservation transaction in `services/images/upload.py`. Return the existing `CONFLICT` category and map it at the MCP wrapper to an ordered `images.delete` then `images.upload` recovery hint; leave ADR-0525 attempt-specific keys and publication fencing unchanged.

**Tech Stack:** Python 3.14, psycopg async SQL, FastMCP/Pydantic tool wrappers, pytest, `uv`, `ruff`, `ty`, and `just` guardrails.

## Global Constraints

- Branch `feat/reject-duplicate-image-upload-1756` is based on `main`; do not implement on the default branch.
- Private registered identity follows the existing database index: `(owner, provider, name)`, without architecture.
- The registered-name check runs under `LockScope.PROJECT` before quota accounting, pending-row mutation, and published-prefix object writes.
- The conflict lookup is owner- and private-visibility-scoped, parameterized, and discloses no row id, object key, digest, principal, or other tenant.
- A sequential duplicate returns `ErrorCategory.CONFLICT`; its registered row and object remain unchanged and digest-consistent.
- An overlapping first upload may write only its own attempt-specific key; exactly one row registers and its object must match its digest. Do not redesign ADR-0525 recovery.
- Do not add a dependency, migration, public-publication behavior, atomic replacement path, or historical repair.
- Guardrails are focused `uv run python -m pytest` commands followed by `just ci`.

---

### Task 1: Reject a registered private identity in the reservation transaction

**Files:**
- Modify: `tests/services/images/test_upload.py`
- Modify: `src/kdive/services/images/upload.py`

**Interfaces:**
- Consumes: `_reserve_under_quota(conn, *, request, project, principal, new_bytes)` and the existing `image_catalog_one_private` identity `(owner, provider, name)`.
- Produces: `RegisteredPrivateNameConflict`, a service-specific `CategorizedError` subtype whose
  category is always `CONFLICT`, and `_registered_private_name_conflict(conn, request) ->
  RegisteredPrivateNameConflict | None`, called while the PROJECT advisory lock is held.

**Acceptance criteria:** A sequential duplicate is rejected with `CONFLICT` before a published-prefix PUT or catalog mutation; the original row fields and object bytes remain unchanged and the object SHA-256 matches the row digest. A same name in another project or a public row does not conflict. Existing concurrent same-identity coverage remains green and proves the registered row/object invariant.

- [x] **Step 1: Write the sequential regression test**

Add `test_registered_private_name_reupload_conflicts_before_publish` beside the other registration tests in `tests/services/images/test_upload.py`. Register bytes A, snapshot the returned row and `store.puts`, seed bytes B at a second quarantine key, and call `_register` with the same project/provider/name. Assert:

```python
with pytest.raises(CategorizedError) as err:
    await _register(
        conn,
        store,
        name="myrootfs",
        quarantine_key="uploads/q/proj/replacement.qcow2",
    )
assert err.value.category is ErrorCategory.CONFLICT
assert "images.delete" in str(err.value)
assert "images.upload" in str(err.value)
assert store.puts == puts_after_first
```

Read the row back through `IMAGE_CATALOG.get`, assert it equals the first registered entry, fetch `store._objects[first.object_key]` through the test seam, and assert `"sha256:" + hashlib.sha256(data).hexdigest() == first.digest`. Also assert no second catalog row exists.

- [x] **Step 2: Run the regression test and verify red**

Run:

```bash
uv run python -m pytest tests/services/images/test_upload.py::test_registered_private_name_reupload_conflicts_before_publish -q
```

Expected: FAIL because the second attempt reaches publication/registration instead of returning the typed pre-write conflict.

- [x] **Step 3: Add the registered-identity decision**

In `src/kdive/services/images/upload.py`, add the transport-neutral subtype and a narrow query
helper using the cursor's default row shape:

```python
class RegisteredPrivateNameConflict(CategorizedError):
    """A private upload collided with its project's registered name."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"private image {name!r} is already registered in this project; "
            "delete it with images.delete, wait for deletion, then retry images.upload",
            category=ErrorCategory.CONFLICT,
        )


async def _registered_private_name_conflict(
    conn: AsyncConnection, request: PublishRequest
) -> RegisteredPrivateNameConflict | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE owner = %s AND provider = %s AND name = %s "
            "AND visibility = %s AND state = %s LIMIT 1",
            (
                request.owner,
                request.provider,
                request.name,
                ImageVisibility.PRIVATE.value,
                ImageState.REGISTERED.value,
            ),
        )
        if await cur.fetchone() is None:
            return None
    return RegisteredPrivateNameConflict(request.name)
```

Call it as the first operation inside `_reserve_under_quota`'s existing PROJECT-locked transaction. Raise the returned conflict before `_project_usage`, `_quota_denial`, or `reserve_publish`. Update the module and function docstrings to distinguish registered-name rejection from pending-row adoption and cite ADR-0526.

- [x] **Step 4: Run the sequential and concurrency service tests**

Before running them, strengthen `test_concurrent_same_identity_uploads_cannot_both_register` so
both contenders use one `_ContendedStore` seeded with both quarantine keys rather than two stores
whose dictionaries are copied. Pass that same store to both `_one` calls. After the race, read the
sole registered row's `object_key`, fetch its bytes from the shared store, and assert:

```python
assert rows[0].object_key is not None
registered_bytes = store._objects[rows[0].object_key]  # noqa: SLF001 - integrity test seam
assert "sha256:" + hashlib.sha256(registered_bytes).hexdigest() == rows[0].digest
```

This assertion is the concurrent acceptance proof: cardinality and row identity alone do not catch
a registered winner whose object contains a losing attempt's bytes.

Run:

```bash
uv run python -m pytest \
  tests/services/images/test_upload.py::test_registered_private_name_reupload_conflicts_before_publish \
  tests/services/images/test_upload.py::test_concurrent_same_identity_uploads_cannot_both_register \
  tests/services/images/test_upload.py::test_private_shadows_public_on_same_provider_name -q
```

Expected: 3 passed. The concurrent test uses a genuinely shared namespace and proves the row/object
digest invariant; the public-shadow test is the control that public visibility does not trigger the
private-name conflict.

- [x] **Step 5: Add a cross-owner control if the regression does not already exercise one**

Add `test_registered_private_name_conflict_is_owner_scoped` only if the sequential regression lacks this explicit check. Register `shared` for `proj`, upload `shared` for `other` using a second quarantine key, and assert both entries register under their respective owners. Do not introduce a reusable helper solely for this second occurrence.

- [x] **Step 6: Run the focused service module and static checks**

Run:

```bash
uv run python -m pytest tests/services/images/test_upload.py -q
just lint
just type
```

Expected: all pass with zero warnings.

- [x] **Step 7: Commit the service behavior**

Stage only `src/kdive/services/images/upload.py` and `tests/services/images/test_upload.py`, then
commit. The test file includes both the sequential regression and the strengthened concurrent
integrity proof:

```bash
git commit -m "fix(images): reject registered private image names"
```

### Task 2: Expose conflict recovery in the MCP contract

**Files:**
- Modify: `tests/mcp/ops/test_images_tools.py`
- Modify: `src/kdive/mcp/tools/ops/images/upload.py`

**Interfaces:**
- Consumes: `RegisteredPrivateNameConflict` from `register_private_upload`, other
  `CategorizedError` failures, and `ToolResponse.failure_from_error`.
- Produces: a failure envelope with `suggested_next_actions == ["images.delete", "images.upload"]` for upload conflicts, plus wrapper docstring text naming the outcome and ordered recovery.

**Acceptance criteria:** The registered-name conflict maps to a `CONFLICT` envelope, the next
actions are literal tool names in recovery order, and the FastMCP-visible wrapper docstring tells
an agent to delete, wait for deletion, then upload again. Publication-supersession conflicts and
other error categories retain their existing empty action list.

- [x] **Step 1: Write the MCP mapping test**

In `tests/mcp/ops/test_images_tools.py`, monkeypatch `image_upload.register_private_upload` to raise:

```python
raise RegisteredPrivateNameConflict("custom")
```

Call `image_upload.upload` as a project operator and assert:

```python
assert response.error_category == ErrorCategory.CONFLICT.value
assert response.suggested_next_actions == ["images.delete", "images.upload"]
```

Also inspect the registered `images.upload` tool description (using the existing FastMCP registrar
pattern) and assert it contains `CONFLICT`, `images.delete`, the wait requirement, and
`images.upload`. Add a control that monkeypatches the service to raise a plain
`CategorizedError(..., category=ErrorCategory.CONFLICT)` representing publication supersession and
assert its `suggested_next_actions` is empty.

- [x] **Step 2: Run the MCP test and verify red**

Run:

```bash
uv run python -m pytest tests/mcp/ops/test_images_tools.py::test_upload_conflict_exposes_delete_then_retry -q
```

Expected: FAIL because `_register_upload` currently maps every typed error without recovery actions and the wrapper does not document the duplicate-name contract.

- [x] **Step 3: Map only conflicts to the recovery actions**

Import `RegisteredPrivateNameConflict`. In `_register_upload`, catch it before the generic
`CategorizedError` clause and supply recovery actions only for that subtype:

```python
except RegisteredPrivateNameConflict as exc:
    return ToolResponse.failure_from_error(
        request.name,
        exc,
        suggested_next_actions=["images.delete", "images.upload"],
    )
except CategorizedError as exc:
    return ToolResponse.failure_from_error(request.name, exc)
```

Update the decorated/wrapper-facing `upload` docstring, not only the inner helper, to state that an
already registered private project/name returns `CONFLICT` before a published object write; the
caller deletes with `images.delete`, waits for deletion to complete, then retries `images.upload`.
Keep the parameter descriptions accurate and avoid adding a new field or tool.

- [x] **Step 4: Run focused MCP and service tests**

Run:

```bash
uv run python -m pytest \
  tests/mcp/ops/test_images_tools.py::test_upload_conflict_exposes_delete_then_retry \
  tests/services/images/test_upload.py::test_registered_private_name_reupload_conflicts_before_publish -q
just lint
just type
```

Expected: all pass with zero warnings.

- [x] **Step 5: Commit the MCP contract**

Stage only `src/kdive/mcp/tools/ops/images/upload.py` and `tests/mcp/ops/test_images_tools.py`, then commit:

```bash
git commit -m "docs(images): expose duplicate upload recovery"
```

### Task 3: Verify the complete issue contract

**Files:**
- Modify only if generated docs or guardrails require a source-derived update.

**Interfaces:**
- Consumes: the committed service behavior, MCP contract, ADR-0526, and design spec.
- Produces: a guardrail-green branch whose diff contains no unrelated generated or environment artifacts.

**Acceptance criteria:** Focused sequential/concurrent/MCP proofs pass, tests bite when the registered-name check is removed, and the repository PR gate is green.

- [x] **Step 1: Prove the sequential test bites**

Temporarily bypass the `_registered_private_name_conflict` result at its call site, run:

```bash
uv run python -m pytest tests/services/images/test_upload.py::test_registered_private_name_reupload_conflicts_before_publish -q
```

Expected: FAIL because the second upload reaches a write or registration conflict. Restore the implementation using `git restore -p` or an inverse patch; do not leave the mutation in the worktree.

Inspect the strengthened concurrent test before final verification and confirm both `_one` calls
receive the same store instance and its post-race digest assertion reads the sole registered row's
exact `object_key`; copied dictionaries or a digest check against a caller-selected key do not
satisfy the proof.

- [x] **Step 2: Re-run the focused contract suite**

Run:

```bash
uv run python -m pytest \
  tests/services/images/test_upload.py::test_registered_private_name_reupload_conflicts_before_publish \
  tests/services/images/test_upload.py::test_concurrent_same_identity_uploads_cannot_both_register \
  tests/mcp/ops/test_images_tools.py::test_upload_conflict_exposes_delete_then_retry -q
```

Expected: 3 passed.

- [x] **Step 3: Run the complete repository guardrail**

Run bare:

```bash
just ci
```

Expected: exit 0 with lint, type, shell/workflow/doc/config/schema/generated guards and the non-live test suite green. Environment-gated live tiers remain outside this issue.

- [x] **Step 4: Re-read the complete diff and commit any required generated update separately**

Run `git diff --check`, `git status --short --untracked-files=all`, and `git diff main...HEAD`. Confirm the branch changes only the frozen surface, the wrapper docstring is agent-visible, no migration or dependency changed, and no Node/Python environment artifact is tracked. If a guardrail required a generated artifact, stage that explicit path and commit it with an imperative Conventional Commit subject; otherwise create no empty commit.
