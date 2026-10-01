# Image-catalog and receipt stored-row faults: implementation plan (#3044)

**Goal:** a corrupt `image_catalog` row or a corrupt module-attempt receipt row raises
`ServerFaultError`. A tool call that reaches either one returns the `infrastructure_failure`
envelope instead of an argument error.

**Architecture:** four one-line rebuild substitutions to `validate_stored`. The kdump gate
surfaces the fault and adds no handler; its docstrings say so. ADR-0709 gets an append-only
amendment. Spec:
[2026-10-01-catalog-receipt-stored-row-fault-3044-design.md](../specs/2026-10-01-catalog-receipt-stored-row-fault-3044-design.md).

Expected implementation size: 90–130 changed lines (S) — four substitutions plus imports,
two docstring edits, a 15-line ADR amendment and about 80 lines of tests in two files.

## Global Constraints

- Import `validate_stored` from `kdive.serialization`. `kdive.db` and `kdive.images` import
  nothing from `kdive.mcp`.
- ADR-0709 is append-only. Insert one `### Amendment (2026-10-01): ... (#3044)` block after the
  #3009 amendment and before `## Considered & rejected`, and change no existing line.
- Guardrails: `just format`, `just lint`, `just type`, focused `just test-verbose <paths>`, and
  `git fetch origin main && just records` for the ADR.

## File map

| File | Change |
|---|---|
| `src/kdive/images/cataloging/catalog.py` | 3 rebuilds use `validate_stored`; the `resolve_system_catalog_rootfs` docstring adds the fault |
| `src/kdive/db/remote_module_attempt_obligations.py` | the receipt rebuild uses `validate_stored` |
| `src/kdive/mcp/tools/lifecycle/vmcore/_vmcore_kdump_gate.py` | docstring only: a corrupt row propagates |
| `docs/adr/0709-invalid-envelope-is-a-server-fault.md` | amendment |
| `tests/db/test_repository_stored_fault.py` | catalog and receipt tests |
| `tests/mcp/lifecycle/test_vmcore_tools.py` | `vmcore.fetch` envelope test |

## Task 1: catalog resolvers and the kdump gate (criteria 1, 3, 4, 6)

**Interfaces:** consumes `validate_stored(model, value) -> M` and `ServerFaultError`
(`src/kdive/serialization.py`); changes no signature.

**Verification:**
- Contract: each of the three resolvers raises `ServerFaultError` over a corrupt row.
  Mode: focused-test. Test: `test_catalog_rebuild_of_a_corrupt_row_is_a_server_fault`. Red:
  `ValidationError` is raised instead of `ServerFaultError`. Green:
  `just test-verbose tests/db/test_repository_stored_fault.py`.
- Contract: `vmcore.fetch` over a corrupt catalog row returns the `infrastructure_failure`
  envelope and enqueues no job. Mode: focused-test. Test:
  `test_fetch_vmcore_over_a_corrupt_catalog_row_is_a_server_fault_envelope`. Red:
  `result.is_error` is true and the text is raw validation text (checked on main). Green:
  `just test-verbose tests/mcp/lifecycle/test_vmcore_tools.py`.
- Contract: the gate docstring states the decision. Mode: task-test-not-applicable; no
  executable consumer reads docstring prose.

Steps:
1. Add to `tests/db/test_repository_stored_fault.py` a test that inserts the
   `IMAGE_CATALOG.insert` entry (copy `_entry()` from `tests/images/test_catalog_resolver.py`
   with `name="base"`), then runs `UPDATE image_catalog SET capabilities = '{bogus}'`. For each
   resolver (`resolve_rootfs(conn, "local-libvirt", "base", project="proj")`,
   `resolve_system_catalog_rootfs(conn, <catalog System stub for "base">)`, and
   `resolve_public_rootfs_sync` over a sync `psycopg.connect`), assert
   `pytest.raises(ServerFaultError, match="stored ImageCatalogEntry failed")` and a
   `ValidationError` `__cause__`.
2. Add to `tests/mcp/lifecycle/test_vmcore_tools.py` a test that seeds
   `_catalog_rootfs_run(pool, capabilities=["kdump"], provenance={})`, corrupts `capabilities`
   the same way, and registers `@app.tool(name="vmcore.fetch")` wrapping
   `_real_local_handlers().fetch_vmcore(pool, _ctx(), run_id=run_id, method="kdump")` on a
   `FastMCP` with `ServerFaultMiddleware()`. Call it through `Client(app)` with
   `raise_on_error=False`. Assert that `not result.is_error`,
   `error_category == "infrastructure_failure"`, `detail == SERVER_FAULT_DETAIL`,
   `_job_count(pool) == 0`, and that no `Invalid arguments for tool` record is on the `fastmcp`
   logger. Use the same caplog handler wiring as
   `tests/mcp/catalog/test_resources_tools.py::test_describe_over_a_corrupt_row_is_a_server_fault_envelope`.
3. Confirm both red failures.
4. In `catalog.py`, add `from kdive.serialization import validate_stored` and replace each
   `ImageCatalogEntry.model_validate(row)` with `validate_stored(ImageCatalogEntry, row)`.
   Append to the `resolve_system_catalog_rootfs` docstring: "A visible row that fails its
   rebuild is not a gap: it raises ``ServerFaultError`` (ADR-0709)."
5. In the `refusing_kdump_capability` docstring, after the resolution-gap list, add: "A corrupt
   catalog row is not a gap. The resolver's ``ServerFaultError`` propagates, so the call
   reports a server fault instead of admitting a capture over a catalog record the server
   cannot read (ADR-0709, #3044)."
6. Run both files green, then `just lint` and `just type`, and commit
   `fix(images): report a corrupt image-catalog row as a server fault`.

## Task 2: module-attempt receipt (criteria 2, 4, 6)

**Interfaces:** consumes `validate_stored` and the test helpers `_seed`, `_attempt` and
`_evidence` (`tests/db/remote_module_attempt_obligations_support.py`), plus the repository
methods `open_mutation_obligation`, `record_terminal_evidence`, `open_reap_obligation` and
`read_reap_preparation(conn, system_id, run_id)`.

**Verification:**
- Contract: a corrupt receipt row raises `ServerFaultError` and reaches a FastMCP caller as
  `infrastructure_failure`. Mode: focused-test. Test:
  `test_receipt_rebuild_of_a_corrupt_row_is_a_server_fault`. Red: the raw
  `ValidationError` for the `operation_nonce` pattern (checked on main). Green:
  `just test-verbose tests/db/test_repository_stored_fault.py`.

Steps:
1. Write the test on an autocommit connection: `_seed`, open the mutation obligation, record
   `_evidence`, and open the reap obligation. Inside
   `async with conn.transaction(force_rollback=True):` run
   `ALTER TABLE remote_module_attempt_obligations DISABLE TRIGGER USER, DROP CONSTRAINT
   remote_module_attempt_nonce, DROP CONSTRAINT remote_module_attempt_evidence_ownership`
   followed by `UPDATE remote_module_attempt_obligations SET operation_nonce = 'A' * 32`. The
   rollback restores the schema for later tests on the worker. In the same block, assert
   `ServerFaultError` with a `ValidationError` cause from `read_reap_preparation`. Then call a
   `FastMCP` tool wrapping the same read behind `ServerFaultMiddleware` and assert that the
   envelope's `error_category` is `infrastructure_failure`.
2. Confirm red, then change line 272 to
   `module_attempt_obligation=validate_stored(ModuleAttemptObligationReceiptV1, rows[0])` and
   import `validate_stored` from `kdive.serialization`.
3. Run green with `tests/db/test_remote_module_attempt_obligations.py` and
   `tests/jobs/handlers/external_boot/test_prepared_before_admission.py` (valid rows), lint,
   type, and commit `fix(db): report a corrupt module-attempt receipt as a server fault`.

## Task 3: ADR-0709 amendment (criterion 5)

**Verification:** Contract: the amendment's decision text. Mode: task-test-not-applicable.
No executable consumer reads the amendment's prose. `just records` still checks the record
shape after `git fetch origin main`.

Steps: append the amendment: both sites join the allowlist (narrowing #3009's "keep their
current path" paragraph); the kdump gate surfaces the fault (the spec's rationale in two
sentences); `complete_build` still fails open; provisioning is unaffected; the provider-layer
authority repository keeps its path. Commit `docs(adr): amend ADR-0709 for catalog and receipt
rebuilds`.
