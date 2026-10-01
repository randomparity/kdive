# Repository stored-row rebuilds are server faults (#3009)

## Problem

ADR-0709's #2981 amendment reports a failed rebuild of the server's own stored data as a server
fault, but only at an allowlist of MCP tool sites that call `validate_stored`. The repository
layer (`src/kdive/db/`) rebuilds rows with `model_validate` directly, so a corrupt row there
raises a bare pydantic `ValidationError`. FastMCP logs that as `Invalid arguments for tool` and
returns the raw validation text as the caller's argument error. The amendment scopes these sites
out explicitly.

## Design

**Ownership move.** `ServerFaultError` and `validate_stored` move from `kdive.mcp.responses` to
`kdive.serialization`, the module that already holds the JSON contracts shared by the database
and MCP boundaries. `kdive.db` may not import `kdive.mcp`, and both layers import
`kdive.serialization` already. `kdive.mcp.responses` imports both names back. It needs
`ServerFaultError` as the base of `InvalidEnvelopeError`, and it re-exports `validate_stored`, so
the nine tool modules, `ServerFaultMiddleware`, and the existing tests keep their imports.
Re-pointing those imports is churn with no behavioural effect, so this change leaves them.

**Rebuild sites.** Every `model_validate` call that rebuilds a stored row in these files goes
through `validate_stored`:

- `src/kdive/db/repositories.py`: 12 sites. These are `InvestigationBuildRepository.insert`,
  `get`, and `active_by_digest`; `Repository.insert`, `get`, and `list_all`;
  `StatefulRepository.update_state`; `KeyedRepository.upsert`; the two returns in
  `ArtifactRepository.claim`; `snapshot_by_name`; and `snapshots_for_system`.
- `src/kdive/db/external_boot_activations.py`: 9 sites. These are `_activation`, plus the
  reservation, recovery-attempt, materialization, and release rebuilds.
- `src/kdive/db/external_boot_authority_journal.py`: the `_binding` rebuild of
  `preparation_plan`.

An `INSERT/UPDATE ... RETURNING` readback counts as stored data. The model being written was
validated before the write, so a readback that fails validation is the server's fault.

`_binding`'s own non-pydantic `ValueError`s (`_uuid`, `_bounded`, `_positive`,
`AuthorityOperation`) are unchanged. They are not model rebuilds.

**Caller migration.** A survey of `src/` found two narrow handlers that caught the old
`ValidationError` (a `ValueError`) from these rebuilds. Each gains `ServerFaultError` so that its
documented behaviour holds:

- `mcp/tools/lifecycle/runs/complete_build.py` `_target_os_id`
  (`except psycopg.Error, ValidationError`) fails open after the build commits (ADR-0678).
- `providers/external_boot_authority/service.py` `readiness`
  (`except AuthorityServiceError, OSError, ValueError`) fails closed, returning `False`.

The handler-free paths change as intended:

- The `except Exception` handlers in the reconciler, the worker, and the job handlers already
  catch `ServerFaultError`.
- An uncaught rebuild in a tool body now reaches `ServerFaultMiddleware`.

None of these need code changes.

`BindingErrorMiddleware` and `tools.invoke` used to relabel a repository `ValidationError` as a
`configuration_error`. Those failures now report as `infrastructure_failure`, which is the
outcome this issue asks for.

**List-tool isolation.** None of the list tools wraps a repository rebuild in its per-row
`except ValueError`:

- `allocations.list` and `resources.list` build their models directly.
- `artifacts.list` only builds envelopes.

All three keep their per-row isolation unchanged.

**ADR.** ADR-0709 gains a dated amendment that widens the allowlist to the repository layer and
records the helper's new home. The amendment is append-only, and no ADR number is assigned.

## Failure model

- **Actors and deployments:** MCP callers of the server's tools; the worker, the reconciler, and
  the job handlers that read the same repositories; an operator reading server logs.
- **Invariants and assets at stake:**
  - A caller-input rebuild keeps FastMCP's argument-error path.
  - `_target_os_id` keeps failing open.
  - `readiness` keeps failing closed.
  - `kdive.db` imports nothing from `kdive.mcp`.
- **Accepted failure classes:**
  - Some non-MCP worker callers record `str(exc)` in a failure message. That text changes to the
    `stored <Model> failed validation` form, and the category is unchanged.
  - The rebuild in `providers/external_boot_authority/repository.py:79` stays a bare
    `ValidationError`. It is outside the named files, and that layer is out of scope.
- **Covered elsewhere:**
  - Caller-input validation stays with the operator (an exclusion).
  - Worker-only rebuilds outside `kdive.db` stay with the operator (an exclusion).
  - The general denylist relabel stays rejected (ADR-0709).

## Success

1. `kdive.serialization` defines `ServerFaultError` and `validate_stored`. The names imported
   from `kdive.mcp.responses` are the same objects.
2. The 22 rebuild sites listed under **Rebuild sites** raise `ServerFaultError`, chained from the
   `ValidationError`, when a stored row is invalid.
3. A tool call through FastMCP that rebuilds an invalid stored row returns an
   `infrastructure_failure` envelope with `SERVER_FAULT_DETAIL`. FastMCP logs no
   `Invalid arguments for tool` record for it.
4. `_target_os_id` returns `None`, and `readiness` returns `False`, on a `ServerFaultError`.
5. ADR-0709 carries the amendment.

## Validation

- **Contract 1 (ownership, identity), `focused-test`:** `tests/mcp/core/test_responses.py`
  asserts that `kdive.mcp.responses.ServerFaultError is kdive.serialization.ServerFaultError`
  and that the same holds for `validate_stored`. It is red before the move.
- **Contract 2 (repository fault), `focused-test`:** a new
  `tests/db/test_repository_stored_fault.py` seeds a resource row with `capabilities = '[]'` and
  asserts `ServerFaultError` with a `ValidationError` cause from `RESOURCES.get` and
  `RESOURCES.list_all` (the generic `Repository` rebuild every module-level instance shares).
  The other sites are the same one-line substitution and get no per-site test. The migrated
  `tests/jobs/handlers/external_boot/test_prepared_before_admission.py` assertion covers
  `_activation`.
- **Contract 3 (tool call), `focused-test`:** `tests/mcp/catalog/test_resources_tools.py` runs the
  real `describe_resource` handler inside a FastMCP app with `ServerFaultMiddleware` over a
  corrupt seeded row. It asserts the envelope and the absence of the argument-error log, and it is
  red before the repository change.
- **Contract 4 (caller behaviour), `focused-test`:**
  - In `tests/mcp/lifecycle/test_complete_build_tool.py`, `resolve_system_catalog_rootfs` is
    monkeypatched to raise `ServerFaultError`, and the run still succeeds with
    `guest_family == "unknown"`.
  - In `tests/providers/external_boot_authority/test_service.py`, the repository's
    `resolve_allocating` raises `ServerFaultError`, and `readiness` returns `False`.
- **Contract 5 (ADR text), `task-test-not-applicable`:** the change is prose with no executable
  consumer. `just records` checks the record's shape.
