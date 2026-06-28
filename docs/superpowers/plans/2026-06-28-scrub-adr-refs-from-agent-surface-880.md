# Scrub ADR refs from the agent-facing MCP surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove internal `ADR-NNNN` citations from every agent-rendered MCP string, stop
serving an ADR as an MCP resource, and add a CI-gated guard that fails if a ref reappears.

**Architecture:** TDD, guard-first. Write the guard (`tests/mcp/core/test_no_adr_leak.py`)
that builds the live FastMCP app and walks every rendered string; its failure output is the
exact worklist. Scrub each source string (provenance moves to non-rendered comments/module
docstrings) until the guard is green, then regenerate the committed tool reference and
doc-resource snapshots.

**Tech Stack:** Python 3.14, `uv`, FastMCP, pytest, `ruff`, `ty`, `just`.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (`just type`).
- No schema, migration, RBAC, persistence, or config change.
- Provenance for maintainers lives only in non-rendered locations (module docstrings or `#`
  comments), never in a string FastMCP renders.
- Prose-style guard (ADRs/specs/comments): no "critical/crucial/essential/significant/
  comprehensive/robust/elegant"; "Milestone" never "Sprint".
- Guardrails before each commit: `just lint`, `just type`, and the focused test(s). Before
  push: full `just test`, `just docs-check`, `just resources-docs-check`.
- `ADR-\d+` is the leak pattern (case-sensitive, matches the issue's `"ADR-"`).

---

### Task 1: The failing guard (TDD red)

**Files:**
- Create: `tests/mcp/core/test_no_adr_leak.py`

**Interfaces:**
- Consumes: `kdive.mcp.app.build_app`; `tests.mcp.conftest.make_keypair, ISSUER, AUDIENCE`;
  `JWTVerifier`, `AsyncConnectionPool`, `SecretRegistry` (same harness as
  `tests/mcp/core/test_tool_docs.py::_build_tools`).
- Produces: `_strings(obj)` recursive string-leaf generator; `_ADR = re.compile(r"ADR-\d+")`.

- [ ] **Step 1: Write the guard.** Build the app once at module load (null pool + local
  keypair verifier). Tests: `test_no_adr_refs_in_tool_surface` (each tool: `description`
  plus every string leaf of `to_mcp_tool().inputSchema`/`outputSchema`),
  `test_no_adr_refs_in_server_instructions` (`app.instructions`),
  `test_no_adr_refs_in_registered_resources` (each resource `name`/`title`/`description`;
  no served URI under `resource://kdive/adr/`), `test_no_adr_refs_in_prompts` (each prompt
  `to_mcp_prompt().description` + argument descriptions), `test_adr_matcher_is_not_vacuous`
  (matcher flags `"see ADR-0019"`; `_strings({"a":{"b":["see ADR-0001"]}})` yields it).

- [ ] **Step 2: Run, confirm RED.** `uv run python -m pytest tests/mcp/core/test_no_adr_leak.py -q`.
  Expected: the four surface tests FAIL listing current leaks; the canary PASSES.

- [ ] **Step 3: Commit.** `test(mcp): add failing ADR-leak guard over the agent surface`.

### Task 2: Scrub the envelope output-schema description

**Files:**
- Modify: `src/kdive/mcp/schema_advertising.py` (`ENVELOPE_OUTPUT_SCHEMA["description"]`).

- [ ] **Step 1.** Drop `(ADR-0019)` from the description string; keep the ADR provenance in
  the existing module-level `#` comment (already cites ADR-0170/0113/0019). Resulting string:
  `"The uniform kdive ToolResponse envelope. \`data\` and \`items\` are intentionally open;
  see resource://kdive/docs/guide/response-envelope.md."`
- [ ] **Step 2.** Re-run the guard: `test_no_adr_refs_in_tool_surface` failure count drops by
  the per-tool `outputSchema` hits (the bulk).
- [ ] **Step 3.** `just lint` + `just type`; commit `refactor(mcp): drop ADR ref from envelope output-schema description`.

### Task 3: Scrub tool descriptions

**Files:**
- Modify: `src/kdive/mcp/tools/gateway.py` (`tools.invoke` docstring),
  `src/kdive/mcp/tools/lifecycle/runs/composite.py` (`runs.build_install_boot` docstring),
  `src/kdive/mcp/tools/ops/build_hosts/build_envs.py` (`build_envs.list` docstring).

- [ ] **Step 1.** Remove the `(ADR-NNNN …)` parentheticals from each tool docstring, fixing
  grammar so the sentence still reads. Where a docstring was the only provenance, add a `#`
  comment or keep the ADR in the module docstring.
- [ ] **Step 2.** Re-run the guard; these three tool-description hits clear.
- [ ] **Step 3.** `just lint` + `just type`; commit `refactor(mcp): drop ADR refs from tool descriptions`.

### Task 4: Scrub field / schema descriptions

**Files (confirm exact strings from the guard's path output):**
- Modify: `src/kdive/mcp/provider_schema.py`, `src/kdive/mcp/tool_payloads.py`,
  `src/kdive/profiles/provider_sections.py`, `src/kdive/profiles/provisioning.py`,
  `src/kdive/profiles/build.py`, `src/kdive/domain/capacity/state.py` (the
  `AllocationState`/`SystemState`/`RunBuildState` class docstrings Pydantic renders into the
  `state` filter schemas), and the `resources.register_*` `vcpus` + `runs.complete_build`
  `cmdline` field descriptions.

- [ ] **Step 1.** For each path the guard reports, remove the `ADR-NNNN` parenthetical from
  the `Field(description=...)` / class docstring, fixing grammar. Move provenance to an
  adjacent `#` comment (Field) or keep it in the module docstring (enum/class). Preserve the
  substrings existing `test_tool_docs.py` guards assert (e.g. build_profile `provenance`/
  `KDIVE_KERNEL_SRC`/`build_provenance`; cmdline `dhash_entries=1`; boot-failure contract).
- [ ] **Step 2.** Re-run the guard until `test_no_adr_refs_in_tool_surface` passes; run
  `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q` to confirm no content guard
  regressed.
- [ ] **Step 3.** `just lint` + `just type`; commit `refactor(mcp): drop ADR refs from schema field descriptions`.

### Task 5: Remove the adr-0080 resource and scrub resource descriptions

**Files:**
- Modify: `src/kdive/mcp/resources/registrar.py` (remove the `adr-0080` `DocResource`; scrub
  ADR refs from `external-build-upload` + `response-envelope` descriptions and from the
  module docstring's stale "Cited by … (ADR-0080)" claims).
- Delete: `src/kdive/mcp/resources/_content/0080-remote-provisioning-disk-image-profile.md`.

- [ ] **Step 1.** Remove the entry, scrub the two descriptions, delete the snapshot.
- [ ] **Step 2.** Re-run the guard (`test_no_adr_refs_in_registered_resources` passes) plus
  `uv run python -m pytest tests/mcp/resources/test_doc_resources.py tests/scripts/test_gen_doc_resources.py tests/mcp/core/test_app.py -q`.
- [ ] **Step 3.** `just lint` + `just type`; commit `refactor(mcp): stop serving ADR-0080 as a resource; scrub resource descriptions`.

### Task 6: Regenerate generated artifacts, full guardrails, ship

**Files:**
- Modify (generated): `docs/guide/reference/*.md`.

- [ ] **Step 1.** `just docs` (regenerate tool reference) and `just resources-docs` (refresh
  snapshots). Review the diff: only ADR refs sourced from scrubbed descriptions vanish.
- [ ] **Step 2.** Full guardrails: `just lint`, `just type`, `just test`, `just docs-check`,
  `just resources-docs-check`. All green; the new guard included.
- [ ] **Step 3.** Commit `docs: regenerate tool reference after ADR-ref scrub`.
- [ ] **Step 4.** Branch adversarial review (`/challenge --base main`), security review,
  then PR `Closes #880`; drive to green + mergeable.

## Self-Review

- **Spec coverage:** criterion 1 → Tasks 1-5 (guard + scrubs across tools/schemas/
  instructions/resources/prompts); criterion 2 → Task 5; criterion 3 → Task 1 (+ `just test`
  gating, Task 6); criterion 4 → Task 4 step 2 (content guards) + Task 6 (docs regen, no
  schema/migration change). Covered.
- **Placeholder scan:** scrub strings are guard-driven by design (the guard prints each
  offending `tool.name`+path); this is the worklist mechanism, not a placeholder.
- **Type consistency:** `_strings`/`_ADR` defined in Task 1 and reused throughout; no
  signature drift.
