# Plan — Provisioning-profile discoverability (#451)

- **Spec:** [`../../specs/2026-06-16-provisioning-profile-discoverability.md`](../../specs/2026-06-16-provisioning-profile-discoverability.md)
- **ADR:** [`../../adr/0124-provisioning-profile-discoverability.md`](../../adr/0124-provisioning-profile-discoverability.md)
- **Branch:** `feat/profile-discoverability-451`
- **Execution mode:** direct in this session — three tightly-coupled changes (typed param +
  one middleware + one read tool + generated docs); not worth subagent fan-out.

## Guardrails (run before every commit; full superset before push)

- `just lint` (ruff check + format check)
- `just type` (ty)
- Targeted: `uv run pytest -q tests/mcp/lifecycle/test_systems_tools.py
  tests/mcp/lifecycle/test_systems_profile_examples.py tests/mcp/core/test_tool_docs.py
  tests/mcp/core/test_app.py` (and the middleware test)
- `just docs` (regenerate tool reference) then `just docs-check` + `just config-docs-check`
- Full local gate before push: `just ci` (note: `check-mermaid` may fail locally if node `jsdom`
  is missing — CI provisions it; if that is the *only* failure, note it in the PR and proceed).

## Conventions

- TDD: failing test first, confirm the right-reason failure, minimal impl, refactor green.
- Drive new handlers with injected `RequestContext` / explicit args (the repo unit contract).
- 100-char lines, Google-style docstrings, absolute imports, no relative paths, zero warnings.
- **Coordination:** sibling agent (#452) also edits `registrar.py`. Keep registrar edits minimal
  and localized to the typed-param change so the rebase is clean.

## Task 1 — Type the `profile` parameter (typed-param half, finding A1)

**Files:** `src/kdive/mcp/tools/lifecycle/systems/registrar.py`.

**Steps (TDD):**
1. **Failing tests** (mirror `tests/mcp/core/test_output_schema.py`'s `Client` pattern):
   - **schema:** build the app, read `tools["systems.define"].parameters["properties"]["profile"]`;
     assert it is **not** `additionalProperties: true` — it is the `ProvisioningProfile` object
     schema (`additionalProperties: false`, with `required`/`properties`).
   - **client render + round-trip (proves spike #2 in-tree):** through a `Client`, list tools (the
     client must build a validator for the input schema without error) and call `systems.define`
     with a **valid** remote profile (`disk-image` + `base_image_volume`); assert it does not raise
     a client-side schema error and the call reaches the tool body (a downstream auth/allocation
     error is fine — the point is the input schema rendered and bound). Confirm both fail first
     (param is still `Mapping` → `additionalProperties: true`).
2. **Impl:** change the three `profile:` params (`systems.define`, `systems.provision`,
   `systems.reprovision`) from `ProvisioningProfileInput` to `ProvisioningProfile`. In each tool
   body, convert the bound model back to the handler's mapping shape with
   `dump_profile(profile)` (`mode="json", by_alias=True, exclude_none=True`) before passing it to
   the handler — so `reconcile_profile_sizing`, `parse()`, and `profile_digest` are byte-identical
   to the old raw-mapping path. Keep `Field(description=...)` text.
3. **Regression test:** a typed-param submission stores the same profile/digest as the equivalent
   raw-mapping submission would (drive the admission path or assert `dump_profile(parsed)` equals
   the raw mapping for a representative profile, including the alias-keyed provider section).
4. Refactor green; run lint/type/targeted tests.

## Task 2 — Re-envelope the boundary `ValidationError` (finding A2)

**Files:** `src/kdive/mcp/middleware.py` (new `ProfileBindingMiddleware`),
`src/kdive/mcp/app.py` (register it innermost), `tests/mcp/core/test_middleware*.py` (new test).

**Steps (TDD):**
1. **Failing test:** call `systems.define` through a `Client` with a malformed `profile`
   (e.g. `{"schema_version": 1}`); assert the result is the `configuration_error` envelope —
   `status == "error"`, `error_category == "configuration_error"`, non-empty `detail`, and an
   `errors` list of `{loc, msg, type}` field paths — **not** a raised `ToolError`. Confirm it
   fails first (raw `ToolError`).
2. **Impl `ProfileBindingMiddleware`:** wrap `call_next`; catch `pydantic.ValidationError`; if
   `context.message.name` is in the fixed set `{systems.define, systems.provision,
   systems.reprovision}`, build `CategorizedError("invalid provisioning profile",
   CONFIGURATION_ERROR, details={"errors": exc.errors(include_url=False, include_input=False,
   include_context=False)})` and return `ToolResponse.failure_from_error(object_id, err)`. Resolve
   `object_id` via a **per-tool id-key map** (`systems.define`/`systems.provision` → `allocation_id`,
   `systems.reprovision` → `system_id`), reading `context.message.arguments.get(key)`; fall back to
   the tool name when absent. Re-raise anything else / any other tool.
3. **Register innermost:** add it in `build_app` **after** `TelemetryMiddleware` and
   `DenialAuditMiddleware` so the binding error becomes a returned envelope inside the telemetry
   span (counted as a normal completion, like a body-rejected profile).
4. **Edge tests:** a malformed profile on a *non*-typed-profile tool is untouched (re-raised); a
   valid profile passes through unchanged; the `errors` list never echoes input values and is
   bounded (reuses `safe_error_details`).
5. Refactor green.

## Task 3 — `systems.profile_examples` discovery tool (finding A3/A4)

**Files:** new `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py` +
`tests/mcp/lifecycle/test_systems_profile_examples.py`; wire `register` in `registrar.py`.

**Design:** a pure projection of `load_inventory_optional(systems.toml)` — no pool. A small builder
takes an `InventoryDoc | None` and the caller `RequestContext` (for the auth-only check via
`current_context()` at the tool wrapper) and returns one `ToolResponse` item per **configured
provider** (a provider with ≥1 declared instance; default set when no file). Each item's
`data["profile"]` is a ready-to-edit dict per the spec's shape table; `data["note"]` flags any
placeholder. `suggested_next_actions = ["systems.define", "allocations.request"]`.

**Reference resolution (leak-safe):** read only provider name and a `PUBLIC`-visibility `[[image]]`
name (remote uses its instance `base_image`, itself a declared image). Never read `uri`/`gdb_addr`/
`gdbstub_range`/`*_cert_ref`. Exclude `private`-visibility images. fault-inject example carries
**no** rootfs. **local-libvirt placeholder uses a `local` rootfs** (absolute placeholder path),
**not** a placeholder `catalog` name — a placeholder catalog name fails
`validate_rootfs_reference` when an inventory file is present (see spec). A `catalog` ref is emitted
**only** when a real `PUBLIC` image exists.

**Steps (TDD):**
1. **Failing tests** driving the builder directly with a synthetic `InventoryDoc`:
   - one example per configured provider; placeholders when no public image.
   - **Validity (AC3):** each example **as emitted** (real-ref or placeholder, no edits) passes
     `ProvisioningProfile.parse()` + `validate_profile_for_provider()`. Obtain `profile_policy` +
     `component_sources` from the provider composition (e.g. local-libvirt's
     `_component_sources()` / `LocalLibvirtProfilePolicy`). **Pin the file coupling:** write one
     temp `systems.toml` (with a `PUBLIC` local-libvirt image + a remote instance), set
     `KDIVE_SYSTEMS_TOML` to it, and drive *both* the builder and the validator off it — because
     `validate_rootfs_reference` re-loads from that env path, not the in-memory doc. Cover both the
     real-`catalog` case (image present) and the `local`-placeholder case (no public image).
     fault-inject example parses with no rootfs.
   - **Leak (AC4):** serialize every example; assert it contains no `uri`/`gdb_addr`/
     `gdbstub_range`/`*_cert_ref` substring and no `private`-image name.
   - no-inventory path returns the default placeholder example set.
2. **Impl** the builder + the `register` wrapper (`@app.tool(name="systems.profile_examples",
   annotations=_docmeta.read_only(), meta={"maturity": "implemented"})`, auth-only via
   `current_context()`, returns `ToolResponse.collection`).
3. Refactor green.

## Task 4 — Wiring + generated docs (finding A5)

**Files:** `tests/mcp/core/test_tool_docs.py` (`_BEHAVIOR_TESTS_BY_TOOL` += `systems.profile_examples`
→ its test file), regenerated `docs/guide/reference/tools.md` (via `just docs`).

**Steps:**
1. Add the tool→test map entry; run `tests/mcp/core/test_tool_docs.py` (the coverage guard).
2. `just docs` to regenerate the tool reference; `just docs-check` + `just config-docs-check` green.

## Sequencing

1 → 2 → 3 → 4. Tasks 1 and 2 are coupled (typed param is what raises the boundary error 2 catches),
so land them together-ish but commit per logical change. Task 3 is independent of 1/2 (ships
regardless per ADR-0124) but registered in the same registrar. Task 4 is last (docs reflect all
new surface).

## Adversarial review

- `/challenge` the spec (done), the plan, and the final branch diff (`--base main`), ≤5 iterations
  each; apply `superpowers:receiving-code-review` to each finding (verify, don't reflexively agree).

## Out of scope / no migration

No DB migration. No `json_schema_extra` fallback (both spikes passed). No project-private DB images.
