# ADR-0284: agent-facing workflow doc system (#940)

- Status: Proposed
- Date: 2026-06-30
- Builds on [ADR-0151](0151-mcp-doc-resources.md) (operator docs as MCP
  resources), [ADR-0202](0202-mcp-lifecycle-prompts.md) (lifecycle prompts),
  [ADR-0268](0268-tool-gateway-dispatcher.md) (tool gateway + composite), and
  [ADR-0269](0269-derive-agent-schemas-from-composed-providers.md) (provider-derived
  exposure). Respects [ADR-0270](0270-no-adr-refs-in-agent-surface.md) (no ADR refs in the
  agent surface).
- Spec: [2026-06-30-issue-940-agent-facing-doc-system-design.md](../archive/superpowers/specs/2026-06-30-issue-940-agent-facing-doc-system-design.md)

## Context

Driving a kdive investigation is slow because the tool surface offers no workflow-shaped
map. An agent discovers each step's tools incrementally (#940, black-box review §8). The
gap is the discovery layer, not the tools.

Four agent-facing surfaces exist, none workflow-shaped: tool schemas (low-level, already
on the wire), three doc resources (build/envelope only, ungated; ADR-0151), three
lifecycle prompts (only reach prompt-listing clients; `build_boot_debug` omits the
`runs.build_install_boot` composite from ADR-0268), and the server instructions namespace
table of contents (one line per namespace, no ordering or "when to use").

Workflow narrative exists in `docs/guide/` but is not served over MCP, and the
per-namespace `docs/guide/reference/*.md` are auto-generated parameter reference — the
low-level layer, not purpose.

## Decision

Publish an agent-facing workflow doc system as MCP doc resources, over existing tools. No
new composite tools or worker jobs.

### 1. Two doc kinds

- **Index docs**: a doc resource is static markdown (identical bytes per caller), so the
  workflow is split into two gated index docs. `docs/guide/agent-index.md`
  (`audience="all"`) is the investigation entry point named in server `instructions`; it
  links only investigation toolset docs and names no operator URI.
  the planned `agent-index-operator.md` (`audience="operator"`) is the operator entry point,
  listed and readable only for platform-operator callers, so it can name operator docs.
- **Per-toolset purpose docs** (`docs/guide/toolsets/<ns>.md`): one per agent-facing
  namespace, explaining each tool by purpose — how it helps an investigation and when to
  reach for it — with an explicit hand-off line directing the agent to the tool's own
  description for parameters and schema. The `runs` doc presents `runs.build_install_boot`
  as the preferred way to run the single-host server-build lane, not as a default over the
  external-upload lane (ADR-0234).

Low-level mechanics and data schema stay single-sourced in the tool docstrings. The
generated `docs/guide/reference/*.md` are unchanged and not served over MCP (an agent
already holds the schema on the wire).

### 2. Dual gating

The `DocResource` model gains `required_kind: ResourceKind | None` (default `None`) and
`audience: Literal["all", "operator"]` (default `"all"`).

- **Provider-static, at registration.** `resources/registrar.register(app, *, resolver)`
  skips an entry whose `required_kind` is not in `resolver.registered_kinds()`. A
  provider-specific doc is absent on a deployment that did not register that provider.
- **Role, at request time.** A new `DocExposureMiddleware` mirrors `ToolExposureMiddleware`
  and gates **both** `on_list_resources` and `on_read_resource`, so an `audience="operator"`
  doc is neither listed nor readable by URI for a caller without the role (a list-only
  filter would leave the doc readable directly). The gate keys on the platform-role axis
  (`ctx.platform_roles` non-empty — the caller holds any platform role), not the
  project-scoped `Role.OPERATOR`, since the operator docs describe platform tools. A strict
  `PLATFORM_OPERATOR` check is avoided because `platform_admin` does not imply
  `platform_operator`, so it would hide the workflow from a platform admin. It is fail-closed
  for the gated subset: on auth error, list returns only `audience="all"` docs and an
  operator-doc read is rejected.

### 3. Coverage and phasing

Investigation subset first (`investigations`, `allocations`, `resources`, `systems`,
`images`, `runs`, `buildconfig`, `build_envs`, `artifacts`, `jobs`, `control`, `debug`,
`introspect`, `vmcore`, `postmortem`), then an operator/admin workflow and its toolset
docs (role-gated). Phase 1 lands the framework plus seed docs for `runs`, `artifacts`,
`debug`, `systems` and the `build_boot_debug` composite fix; Phase 2 fills the remaining
investigation docs; Phase 3 adds the operator set.

### 4. Drift guard and surfacing

Docs are authored under canonical `docs/`, snapshot-packaged by
`scripts/gen_doc_resources.py`, and drift-guarded by the existing `resources-docs-check`.
A completeness test asserts every live tool in a served namespace is named in its doc.
`build_instructions()` points at the index URI; `docs/README.md` lists the served set.

### 5. Prompts kept

The three ADR-0202 prompts are kept and cross-referenced from the index. The
`build_boot_debug` prompt is amended to signpost `runs.build_install_boot`.

## Consequences

- An agent gets a workflow-shaped entry point over a single MCP doc resource, with a
  purpose doc per toolset, while the docstrings stay the one place for parameters.
- Provider-specific docs cannot be listed or read on deployments without that provider;
  operator docs are not listed to non-operator callers. Both gates reuse seams the tool
  surface already uses (`registered_kinds()`, `request_context()`).
- A new `DocExposureMiddleware` is added; it is the first per-connection filter on the
  resource list and read. Gating both paths (and keying on the same platform-operator
  predicate the `ops.*` tools use) keeps an operator doc from being listed or read by a
  caller who could not invoke the tools it describes; fail-closed on the gated subset keeps
  an auth error from leaking it.
- Doc count grows by roughly fifteen investigation docs plus the operator set. The
  completeness guard ties each doc to its namespace so docs cannot silently fall behind the
  tools; the snapshot guard ties the served bytes to canonical `docs/`.
- The original #940 friction is addressed at the discovery layer: the composite is
  signposted in both the `runs` doc and the `build_boot_debug` prompt.
- No schema, migration, RBAC role, or config change. Rollback is reverting the branch.

## Considered & rejected

- **New composite tools** ("console search", "run command in guest", "reprovision
  rootfs"): new worker jobs, RBAC gates, and payloads for a low-priority discovery issue.
  The issue asks for "composites and/or discoverable recipe metadata"; the metadata path is
  the smaller, faithful one. New composites can still be justified later on their own
  merits, as `runs.build_install_boot` was (ADR-0268).
- **Serve the generated `docs/guide/reference/*.md`**: that is parameter reference
  generated from docstrings, which the agent already holds on the wire. Serving it
  duplicates the schema and doubles the drift surface.
- **Fold the prompts away into docs**: prompts are a distinct MCP primitive reaching a
  distinct client capability. Keeping both costs little; the index cross-references them.
  "Replace, don't deprecate" does not apply across primitives.
- **Leave docs ungated**: a local-only deployment would advertise remote-libvirt docs and
  every caller would see the operator workflow. Both gates reuse existing seams at low cost.
- **Embed the full workflow in server instructions**: spends context budget on every
  session and has no place for per-toolset depth. A one-line pointer to the index doc keeps
  instructions lean while reaching every client.
- **Per-request provider gating**: `registered_kinds()` is fixed at composition, so the
  provider gate is decided once at registration. Only the role gate needs request-time
  filtering.
