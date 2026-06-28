# Scrub internal ADR references from the agent-facing MCP surface (#880)

Design record for issue #880. Decision rationale and rejected alternatives live in
[ADR-0270](../adr/0270-no-adr-refs-in-agent-surface.md); this spec is the falsifiable
requirement and the work breakdown.

## Problem

ADR citations (`ADR-NNNN`) leak into the production agent contract because FastMCP renders
tool-function docstrings as tool descriptions and Pydantic `Field(description=...)` /
model/enum docstrings as schema descriptions. Introspecting the live registry today finds
ADR refs in:

- every tool's `outputSchema.description` (one shared envelope string citing `ADR-0019`);
- a handful of tool `description` strings (`tools.invoke`, `runs.build_install_boot`,
  `build_envs.list`);
- input-schema field descriptions — system-profile sub-schemas, allocation/run/system
  state-filter enums, `resources.register_*` `vcpus`, `runs.create` `build_profile`,
  `runs.complete_build` `cmdline`;
- registered-resource descriptions (`external-build-upload`, `response-envelope`) and the
  `adr-0080` resource served wholesale.

The server `instructions` and prompts are already clean (confirmed by introspection).

## Success criteria (falsifiable)

1. **No ADR ref in the rendered contract.** Building the app and walking every
   agent-rendered string — server `instructions`; each tool's `description` and every
   `description`/`title` in its `inputSchema`/`outputSchema`; each registered resource's
   `name`/`title`/`description` — yields zero `ADR-\d+` matches.
2. **No ADR served as a resource.** `ListMcpResources` returns no `resource://kdive/adr/*`;
   `adr-0080` and its packaged snapshot are gone.
3. **The guard is real and CI-gated.** A pytest guard enforces (1), runs under `just test`
   (a per-PR CI step), and a vacuity canary proves its matcher flags a known-bad string.
4. **No behavior regression.** Descriptions still describe what each tool does and how to
   call it (the existing `test_tool_docs.py` content guards — confusable-tool guidance,
   cmdline contract, build_profile provenance, boot-failure contract — stay green). No
   schema/migration/RBAC/config change. `just docs` / `just resources-docs` regenerated and
   in sync.

## Scope of edits

Driven by the guard (write it first; it lists every offending path). Sources:

- `src/kdive/mcp/schema_advertising.py` — `ENVELOPE_OUTPUT_SCHEMA["description"]` (kills the
  bulk).
- Tool docstrings: `mcp/tools/gateway.py` (`tools.invoke`), `mcp/tools/lifecycle/runs/composite.py`
  (`runs.build_install_boot`), `mcp/tools/ops/build_hosts/build_envs.py` (`build_envs.list`).
- Field/schema descriptions: `mcp/provider_schema.py`, `mcp/tool_payloads.py`,
  `profiles/provider_sections.py`, `profiles/provisioning.py`, `profiles/build.py`,
  `domain/capacity/state.py` (state-enum class docstrings), the `resources.register_*` and
  `runs.complete_build` payloads.
- `src/kdive/mcp/resources/registrar.py` — remove the `adr-0080` `DocResource`; scrub ADR
  refs from the `external-build-upload` and `response-envelope` descriptions and from the
  module docstring's stale "Cited by …" claims. Delete
  `src/kdive/mcp/resources/_content/0080-remote-provisioning-disk-image-profile.md`.

For each scrubbed string, ADR provenance moves to the module docstring or an adjacent `#`
comment (non-rendered), per repo convention.

## Guard design

`tests/mcp/core/test_no_adr_leak.py`: build the app with a null pool + local-keypair
verifier (the existing service-test harness), then:

- `test_no_adr_refs_in_tool_surface` — for each tool, collect `description` plus every
  `description`/`title` string anywhere in `to_mcp_tool()`'s `inputSchema`/`outputSchema`;
  assert none match `ADR-\d+`, reporting `tool.name` + JSON path on failure.
- `test_no_adr_refs_in_server_instructions` — assert `app.instructions` is clean.
- `test_no_adr_refs_in_registered_resources` — assert each resource's `name`/`title`/
  `description` is clean and no served URI is under `resource://kdive/adr/`.
- `test_adr_matcher_is_not_vacuous` — assert the shared matcher flags `"see ADR-0019"`, so a
  broken walk cannot pass by matching nothing.

## Out of scope

- Served operator-doc **content bodies** (curated published reference; ADR-0270 rejected).
- `config.md` and other human docs whose ADR refs do not originate from the MCP registry.
- Internal module docstrings/comments that FastMCP does not render.
