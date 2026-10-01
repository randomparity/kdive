# Repository stored-row faults: implementation plan (#3009)

**Goal:** a stored row that fails validation in a `kdive.db` rebuild raises `ServerFaultError`,
which reaches an MCP caller as an `infrastructure_failure` envelope. Today it raises a bare
`ValidationError`, which FastMCP reports as an argument error.

**Architecture:** `ServerFaultError` and `validate_stored` move to `kdive.serialization`, and
`kdive.mcp.responses` re-exports them. The 22 repository rebuild sites call `validate_stored`.
Two narrow callers add `ServerFaultError` to their handlers. ADR-0709 gets an amendment.

**Tech stack:** Python 3, pydantic 2, psycopg 3, FastMCP 3.4.4, pytest with testcontainers
Postgres (the `migrated_url` fixture).

Spec: [2026-09-30-repository-stored-row-fault-3009-design.md](../specs/2026-09-30-repository-stored-row-fault-3009-design.md).

Expected implementation size: 120–190 changed lines (M) — 22 one-line substitutions with their
import lines, a 25-line helper move, two handler edits, about 90 lines of tests across four
files, and a 20-line ADR amendment.

## Global Constraints

- `kdive.db` and `kdive.providers` import nothing from `kdive.mcp`. Import the moved names
  from `kdive.serialization`.
- `ServerFaultError` stays a plain `Exception` subclass. Never make it a `ValueError`; ADR-0709
  explains why.
- The error message stays `stored {model.__name__} failed validation`, chained with `from exc`.
- ADR-0709 is Accepted and append-only. Insert one `### Amendment (2026-09-30): ... (#3009)` block
  at the end of `## Consequences`, after the #2981 amendment and before `## Considered & rejected`
  (`docs/adr/README.md`: an amendment goes in the level-2 section it qualifies). Change no
  existing line.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, and `just records`
  (run `git fetch origin main` first) for the ADR change. Run `just format` before each commit.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/serialization.py` | gains `ServerFaultError` and `validate_stored` | the stored-data fault contract (criterion 1) |
| `src/kdive/mcp/responses.py` | drops both definitions and imports them from `kdive.serialization` | the envelope; re-export point for the tool modules |
| `src/kdive/mcp/middleware/server_fault.py` | the docstring names `kdive.serialization.ServerFaultError` | unchanged |
| `src/kdive/db/repositories.py` | 12 rebuild sites use `validate_stored` | criterion 2 |
| `src/kdive/db/external_boot_activations.py` | 9 rebuild sites use `validate_stored` | criterion 2 |
| `src/kdive/db/external_boot_authority_journal.py` | the `_binding` `preparation_plan` rebuild uses `validate_stored` | criterion 2 |
| `src/kdive/mcp/tools/lifecycle/runs/complete_build.py` | `_target_os_id` also catches `ServerFaultError` | criterion 3 (fails open) |
| `src/kdive/providers/external_boot_authority/service.py` | `readiness` also catches `ServerFaultError` | criterion 3 (fails closed) |
| `docs/adr/0709-invalid-envelope-is-a-server-fault.md` | appended amendment | criterion 5 |
| tests | see each task | — |

## Task 1: move the helper and route the repository rebuilds through it

This is one task. The identity test and the repository fault test both fail until the move and
the substitution land together.

**Interfaces:**

- Produces `kdive.serialization.ServerFaultError(Exception)`.
- Produces `kdive.serialization.validate_stored[M: BaseModel](model: type[M], value: object) -> M`.
- Both names remain importable from `kdive.mcp.responses`, as the same objects.

**Verification:**

- Contract (identity), `Mode: focused-test`. Test: `tests/mcp/core/test_responses.py::test_stored_fault_names_are_owned_below_mcp`.
  Red: `ImportError` on `kdive.serialization.ServerFaultError`. Green:
  `just test-verbose tests/mcp/core/test_responses.py`.
- Contract (repository fault), `Mode: focused-test`. Test:
  `tests/db/test_repository_stored_fault.py`. Red: `ValidationError` raised where
  `ServerFaultError` is expected. Green: `just test-verbose tests/db/test_repository_stored_fault.py`.
- Contract (activation rebuild), `Mode: focused-test`. Test:
  `tests/jobs/handlers/external_boot/test_prepared_before_admission.py`, the case at about
  line 359. Red once the source changes but before the test migrates: `ServerFaultError` is
  raised where `ValidationError` is expected. Green:
  `just test-verbose tests/jobs/handlers/external_boot/test_prepared_before_admission.py`.

**Steps:**

1. Add to `tests/mcp/core/test_responses.py`:

   ```python
   def test_stored_fault_names_are_owned_below_mcp() -> None:
       # kdive.db raises these, and it may not import kdive.mcp (#3009).
       import kdive.serialization as owner
       from kdive.mcp import responses

       assert responses.ServerFaultError is owner.ServerFaultError
       assert responses.validate_stored is owner.validate_stored
   ```

2. Create `tests/db/test_repository_stored_fault.py`:

   ```python
   """A stored row that fails its model rebuild in the repository layer is a server fault (#3009)."""

   from __future__ import annotations

   import asyncio
   from uuid import UUID

   import psycopg
   import pytest
   from pydantic import ValidationError

   from kdive.db.repositories import RESOURCES
   from kdive.providers.core.resource_registration import register_discovered_resource
   from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
   from kdive.serialization import ServerFaultError
   from tests.providers.local_libvirt.fakes import FakeLibvirtConn


   async def _corrupt_resource(conn: psycopg.AsyncConnection) -> UUID:
       discovery = LocalLibvirtDiscovery(
           host_uri="qemu:///system", connect=FakeLibvirtConn, concurrent_allocation_cap=2
       )
       res = await register_discovered_resource(
           conn, discovery.list_resources()[0], pool="local-libvirt", cost_class="local"
       )
       await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb WHERE id = %s", (res.id,))
       return res.id


   def test_repository_rebuild_of_a_corrupt_row_is_a_server_fault(migrated_url: str) -> None:
       async def _run() -> None:
           async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
               res_id = await _corrupt_resource(conn)
               with pytest.raises(ServerFaultError, match="stored Resource failed") as got:
                   await RESOURCES.get(conn, res_id)
               assert isinstance(got.value.__cause__, ValidationError)
               with pytest.raises(ServerFaultError, match="stored Resource failed"):
                   await RESOURCES.list_all(conn)

       asyncio.run(_run())
   ```

   Also write Task 2 step 3's `test_describe_over_a_corrupt_row_is_a_server_fault_envelope`
   now, so that its red is observed before the source change.

   Run `just test-verbose tests/db/test_repository_stored_fault.py tests/mcp/core/test_responses.py`
   and expect both new tests to fail as described under Verification. Then run the describe node
   and expect `result.is_error` to be true.

3. In `src/kdive/serialization.py`, add `from pydantic import BaseModel, ValidationError` beside
   the existing imports. Then append:

   ```python
   class ServerFaultError(Exception):
       """A value the server built failed validation — a server fault, not a caller error (ADR-0709).

       Deliberately not a ``ValueError``: pydantic would re-wrap one raised from a nested model into a
       ``ValidationError``, which FastMCP reports as the caller's argument error, and a tool's
       ``except ValueError`` for caller input could absorb it. Lives here, below the MCP layer, so the
       repository layer can raise it (#3009).
       """


   def validate_stored[M: BaseModel](model: type[M], value: object) -> M:
       """Rebuild ``model`` from the server's own stored data (a database row, a recorded payload).

       Only for values the call's arguments did not supply: a failure is the server's fault and
       raises :class:`ServerFaultError` chained from the pydantic error (ADR-0709, #2981, #3009).
       """
       try:
           return model.model_validate(value)
       except ValidationError as exc:
           raise ServerFaultError(f"stored {model.__name__} failed validation") from exc
   ```

   Update the module docstring's first line to say that it also holds the stored-data fault
   contract.

4. In `src/kdive/mcp/responses.py`:
   - Delete the `ServerFaultError` class and the `validate_stored` function.
   - Change the serialization import to
     `from kdive.serialization import (JsonValue, ServerFaultError, safe_error_details, validate_json_value)`.
   - `validate_stored` is now unused inside `responses.py` and exists there only for importers.
     Import it on its own line as `from kdive.serialization import validate_stored as validate_stored`,
     the explicit re-export form that ruff's F401 accepts.
   - Keep `ValidationError` in the pydantic import, because the `InvalidEnvelopeError`
     translation still uses it.

   Run `rg -n "validate_stored|ServerFaultError" src/kdive/mcp/responses.py` to confirm that only
   the import and the `InvalidEnvelopeError(ServerFaultError)` base remain.

5. In `src/kdive/db/repositories.py`:
   - Change `from kdive.serialization import JsonValue` to
     `from kdive.serialization import JsonValue, validate_stored`.
   - Rewrite each of the 12 calls `X.model_validate(row)` / `self._model.model_validate(row)` /
     `self._model.model_validate(updated)` as `validate_stored(X, row)` /
     `validate_stored(self._model, row)` / `validate_stored(self._model, updated)`. The calls are
     at lines 89, 101, 119, 179, 191, 203, 265, 400, 432, 436, 501, and 512.

   Afterwards, `rg -n "model_validate\(" src/kdive/db/repositories.py` should print nothing.

6. In `src/kdive/db/external_boot_activations.py`:
   - Add `from kdive.serialization import validate_stored`.
   - Rewrite the 9 calls at lines 62, 208, 296, 311, 333, 494, 1052, 1127, and 1218 the same way.
     For example, `ExternalBootMaterialization.model_validate(current["materialization"])`
     becomes `validate_stored(ExternalBootMaterialization, current["materialization"])`.

   Afterwards, `rg -n "model_validate\(" src/kdive/db/external_boot_activations.py` should print
   nothing.

7. In `src/kdive/db/external_boot_authority_journal.py`:
   - Add `from kdive.serialization import validate_stored`.
   - Change `ExternalBootPlan.model_validate(row["preparation_plan"])` to
     `validate_stored(ExternalBootPlan, row["preparation_plan"])`.

8. In `tests/jobs/handlers/external_boot/test_prepared_before_admission.py`:
   - Change `pytest.raises(ValidationError, match="recovery point ownership")` to
     `pytest.raises(ServerFaultError, match="stored ExternalBootActivation failed validation") as raised`.
   - Follow it with `assert "recovery point ownership" in str(raised.value.__cause__)`.
   - Import `ServerFaultError` from `kdive.serialization`, and drop the `ValidationError` import
     if nothing else in the file uses it.

9. In `src/kdive/mcp/middleware/server_fault.py`, change the docstring reference to
   ``:class:`~kdive.serialization.ServerFaultError` ``. The import from `kdive.mcp.responses`
   stays.

10. Run `just test-verbose tests/db/test_repository_stored_fault.py tests/mcp/core/test_responses.py tests/mcp/middleware/test_server_fault.py tests/jobs/handlers/external_boot/test_prepared_before_admission.py tests/db/test_external_boot_activation_repository.py`.
    Expect all to pass. Then run `just lint` and `just type` (exit 0), and commit
    `fix(db): report corrupt stored rows as server faults`.

**Acceptance:**

- No `model_validate(` remains in the three `kdive.db` files.
- `rg -n "kdive.mcp" src/kdive/db src/kdive/serialization.py` prints nothing.

## Task 2: keep the two narrow callers' behaviour, and prove it through a tool call

**Interfaces:** consumes `kdive.serialization.ServerFaultError` from Task 1.

**Verification:**

- Contract (`_target_os_id` fails open), `Mode: focused-test`. Test:
  `tests/mcp/lifecycle/test_complete_build_tool.py::test_a_corrupt_stored_row_in_the_family_lookup_still_completes_as_unknown`.
  Red: `ServerFaultError` propagates out of the tool. Green: `just test-verbose`
  on that node ID.
- Contract (`readiness` fails closed), `Mode: focused-test`. Test:
  `tests/providers/external_boot_authority/test_service.py::test_readiness_fails_closed_on_a_corrupt_stored_binding`.
  Red: `ServerFaultError` propagates out of `readiness`. Green: `just test-verbose` on that node ID.
- Contract (tool call returns the server-fault envelope), `Mode: focused-test`. Test:
  `tests/mcp/catalog/test_resources_tools.py::test_describe_over_a_corrupt_row_is_a_server_fault_envelope`.
  Red on a tree without Task 1: `is_error` with `Invalid arguments for tool`. Green:
  `just test-verbose` on that node ID.

**Steps:**

1. Add to `tests/mcp/lifecycle/test_complete_build_tool.py`, next to
   `test_a_failing_family_lookup_still_completes_as_unknown`:

   ```python
   def test_a_corrupt_stored_row_in_the_family_lookup_still_completes_as_unknown(
       migrated_url: str, monkeypatch: Any
   ) -> None:
       # Stands in for SYSTEMS.get raising ServerFaultError over a corrupt systems row (#3009);
       # the lookup still fails open after the build committed (ADR-0678).
       from kdive.mcp.tools.lifecycle.runs import complete_build as module
       from kdive.serialization import ServerFaultError

       async def _corrupt(conn: Any, system: Any) -> None:
           raise ServerFaultError("stored System failed validation")

       monkeypatch.setattr(module, "resolve_system_catalog_rootfs", _corrupt)
       _config_uploaded(monkeypatch)

       async def _run() -> None:
           async with _pool(migrated_url) as pool:
               resp = await _complete_with_config(
                   pool, _NO_RHEL_KDUMP_SET, target_kind=ResourceKind.LOCAL_LIBVIRT
               )
           assert resp.status == "succeeded", resp
           warning = cast(dict[str, Any], resp.data["rhel_guest_crash_config"])
           assert warning["guest_family"] == "unknown"

       asyncio.run(_run())
   ```

2. Add to `tests/providers/external_boot_authority/test_service.py`, after
   `test_readiness_requires_exact_local_and_trusted_head`:

   ```python
   @pytest.mark.anyio
   async def test_readiness_fails_closed_on_a_corrupt_stored_binding(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> None:
       # The journal's binding rebuild raises ServerFaultError on a corrupt row (#3009).
       service, repository, _, peer, request = _service(tmp_path)

       async def _corrupt(*_: object) -> None:
           raise ServerFaultError("stored ExternalBootPlan failed validation")

       monkeypatch.setattr(repository, "resolve_allocating", _corrupt)
       assert not await service.readiness(peer, request)
       assert service.metrics.recovery_failures == {("untrusted", "unresolved"): 1}
   ```

   Import `ServerFaultError` from `kdive.serialization`. The failure fires before a binding
   resolves, so the metric labels are the untrusted pair. Confirm this against
   `_trusted_labels(None)` in `service.py`, and if it disagrees, assert the value it returns.

3. This test was written in Task 1 step 2. It is shown here in full. Add it to
   `tests/mcp/catalog/test_resources_tools.py` (new imports: `from fastmcp import Client,
   FastMCP`, `from kdive.mcp.middleware.server_fault import SERVER_FAULT_DETAIL,
   ServerFaultMiddleware`):

   ```python
   def test_describe_over_a_corrupt_row_is_a_server_fault_envelope(
       migrated_url: str, caplog: pytest.LogCaptureFixture
   ) -> None:
       # A repository rebuild of a corrupt stored row is a server fault, not an argument error (#3009).
       fastmcp_logger = logging.getLogger("fastmcp")
       fastmcp_logger.addHandler(caplog.handler)
       caplog.set_level(logging.DEBUG)

       async def _run() -> Any:
           async with _pool(migrated_url) as pool:
               res_id = await _register(pool)
               async with pool.connection() as conn:
                   await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb")
               app: FastMCP = FastMCP("t")
               app.add_middleware(ServerFaultMiddleware())

               @app.tool(name="resources.describe")
               async def describe() -> ToolResponse:
                   return await catalog_resources_tools.describe_resource(pool, CTX, res_id)

               async with Client(app) as client:
                   return await client.call_tool("resources.describe", {}, raise_on_error=False)

       try:
           result = asyncio.run(_run())
       finally:
           fastmcp_logger.removeHandler(caplog.handler)
       assert not result.is_error
       envelope = result.structured_content
       assert envelope["error_category"] == ErrorCategory.INFRASTRUCTURE_FAILURE.value
       assert envelope["detail"] == SERVER_FAULT_DETAIL
       assert not [r for r in caplog.records if "Invalid arguments for tool" in r.getMessage()]
   ```

4. Run the three node IDs. Expect the first two to fail with `ServerFaultError` propagating.
   The third passes now, because Task 1 already landed. Its red was observed in Task 1 step 2,
   where it was written before the source change.

5. In `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`:
   - Change `except psycopg.Error, ValidationError:` to
     `except psycopg.Error, ValidationError, ServerFaultError:`.
   - Add `from kdive.serialization import JsonValue, ServerFaultError`, replacing the existing
     `JsonValue` import line.
   - Add a sentence to the docstring: a corrupt stored row (`ServerFaultError`, #3009) fails open
     too.

6. In `src/kdive/providers/external_boot_authority/service.py`:
   - Change `except AuthorityServiceError, OSError, ValueError:` in `readiness` to
     `except AuthorityServiceError, OSError, ValueError, ServerFaultError:`.
   - Add `from kdive.serialization import ServerFaultError`.

7. Run all three node IDs and expect them to pass. Then run
   `just test-verbose tests/mcp/lifecycle/test_complete_build_tool.py tests/providers/external_boot_authority/test_service.py tests/mcp/catalog/test_resources_tools.py`,
   `just lint`, and `just type`, all with exit 0. Commit
   `fix: keep fail-open and fail-closed callers over stored-row faults`.

## Task 3: amend ADR-0709

**Verification:** Contract (ADR text), `Mode: task-test-not-applicable`. The amendment is prose
that no executable consumer reads. `just records` validates the record's shape and its Accepted
status.

**Steps:**

1. Insert at the end of `## Consequences` in `docs/adr/0709-invalid-envelope-is-a-server-fault.md`,
   immediately before `## Considered & rejected`:

   ```markdown
   ### Amendment (2026-09-30): repository-layer rebuilds are server faults (#3009)

   This widens the #2981 allowlist and moves the helper. It narrows that amendment's last
   excluded bullet to rebuilds outside `kdive.db.repositories`,
   `kdive.db.external_boot_activations`, and the authority-journal binding.

   `ServerFaultError` and `validate_stored` now live in `kdive.serialization`, below the MCP layer,
   because `kdive.db` may not import `kdive.mcp`. `kdive.mcp.responses` re-exports both, and
   `InvalidEnvelopeError` still subclasses `ServerFaultError`. Every stored-row rebuild in
   `kdive.db.repositories`, `kdive.db.external_boot_activations`, and the
   `kdive.db.external_boot_authority_journal` binding goes through `validate_stored`, including
   `RETURNING` readbacks. A corrupt row rebuilt at one of these sites during a tool call now reaches the caller as the
   `infrastructure_failure` envelope.

   Two callers relied on the old `ValueError` and now also catch `ServerFaultError`:
   `runs.complete_build`'s guest-OS lookup still fails open (ADR-0678), and the authority
   service's `readiness` still fails closed. Broad `except Exception` handlers in workers and the
   reconciler are unaffected.

   Other stored-row rebuilds keep their current path. These include the worker-side
   `kdive.db.remote_module_attempt_obligations` receipt, the image catalog lookup, and the
   provider-layer authority repository. Caller-input rebuilds keep their current path too. The rejected denylist stays rejected.
   ```

2. Run `git fetch origin main`, then `just records`, and expect exit 0. Commit
   `docs(adr): widen ADR-0709 to repository rebuilds (#3009)`.
