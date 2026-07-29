# ADR 0456 — Define agent and operator MCP exposure profiles

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1577
- **Epic:** #1576
- **Supersedes:** [ADR-0268](0268-tool-gateway-dispatcher.md) §4's one global
  gateway/default exposure switch.
- **Extends:** [ADR-0089](0089-operator-cli-mcp-client.md) decision 5's trusted `kdivectl`
  OIDC client identity.

## Context

KDIVE's MCP registry has grown large enough that normal agents benefit from a compact
gateway-first discovery surface, while `kdivectl` still needs direct tool schemas and live
annotations for generated verbs and safety classification. A single process-wide
`KDIVE_MCP_TOOL_GATEWAY` switch cannot serve both clients: turning it on hides the direct schemas
`kdivectl` uses; leaving it off keeps normal agents on the full catalog.

The distinction is an exposure concern only. It must not introduce a second transport, a second
authentication path, or a new authorization boundary. Execution-time RBAC, destructive-operation
gates, denial audit, and response envelopes remain authoritative in every profile.

## Decision

### 1. Two exposure profiles, selected from trusted identity

KDIVE has two MCP exposure profiles:

- `agent`: compact, gateway-first discovery for normal agent clients.
- `operator`: direct-tool discovery for the authenticated operator CLI.

Profile selection is derived only from verified authenticated context. A request header, tool
argument, query parameter, or client-supplied resource read cannot select the `operator` profile.
The trusted positive signal for `operator` is the dedicated `kdivectl` OIDC client identity already
required by ADR-0089. A token that does not map to that client identity receives the `agent`
profile. If profile resolution fails or the client identity is ambiguous, KDIVE fails toward the
`agent` profile and logs the reason; it never fails open to `operator`.

### 2. Tool-list semantics

Both profiles start from the same RBAC-visible tool set. RBAC filtering remains advisory catalog
hygiene, not the security boundary.

The `agent` profile lists the reviewed gateway core: `tools.search`, `tools.invoke`,
`session.whoami`, and the small lifecycle entry set needed to orient and start work. Tools omitted
from `list_tools` are still reachable through `tools.search` and `tools.invoke`, subject to the
inner tool's normal validation, RBAC, destructive gates, telemetry, and audit behavior.

The `operator` profile lists the caller's full RBAC-visible direct tools. It also includes
`tools.search` and `tools.invoke` as fallbacks, but it does not clip direct schemas to the gateway
core. Direct tools are advertised with their live annotations and maturity metadata so `kdivectl`
can classify read-only, mutating, and destructive verbs from the server's current schema.

### 3. Gateway reachability and search metadata

`tools.search` searches the RBAC-visible catalog for the caller, not merely the tools currently
listed by that caller's profile. This keeps a compact agent `list_tools` from hiding capabilities
that are intentionally gateway-reachable.

Search results must include enough information to invoke and classify the selected operation:
~~projected input schema, description,~~ annotations, and maturity metadata. Retired or
consolidated operation names remain curated search vocabulary for their replacement so agents can
discover the new entry point without compatibility aliases.

*Amended by [ADR-0472](0472-summary-first-tool-search.md) — the projected input schema and the
complete description are returned only for `detail: "full"`; annotations and maturity stay on
every match, so classification remains unconditional.*

### 4. Operator annotation access

`kdivectl` relies on direct `list_tools` annotations for generated-verb dispatch and safety
classification. The operator profile is therefore part of the CLI contract: if the dedicated
`kdivectl` client identity is configured and the token is valid, the CLI receives direct schemas
allowed by its RBAC context. If the client identity is missing or reused from an agent client, the
server exposes the agent profile; `kdivectl doctor` should report that misconfiguration.

### 5. Telemetry and audit

Profile selection is recorded as low-cardinality telemetry (`agent`, `operator`, or `unknown`
before fallback) with the trusted selection source. Tool invocation telemetry and denial audit
continue to record the real inner operation once. The gateway wrapper must not create a second
authoritative audit event for a re-entered inner tool call.

### 6. Default flip gate

The implementation may add profile selection before changing the default agent exposure. Defaulting
normal agents to the compact gateway profile is gated on two real-client proofs recorded in the
default-flip change:

- an agent cold-start proof that discovers and invokes a representative lifecycle through the
  gateway; and
- a `kdivectl` proof that read-only, mutating, and destructive direct tools keep live annotations
  and classify correctly under the operator profile.

Until that evidence exists, the service may keep the current full-catalog default for normal agent
clients even after profile-selection code exists.

**Gate satisfied (2026-07-27, #1582).** Both proofs were run against a live stack with a real
Claude Code client and recorded in
[`2026-07-27-mcp-exposure-profiles-proof-record-1582.md`](../design/2026-07-27-mcp-exposure-profiles-proof-record-1582.md):
a cold-start agent discovered and invoked non-listed tools through the gateway and polled a
provision job to `succeeded`, and `kdivectl` classified read-only, mutating, and destructive direct
tools from live annotations under the operator profile. `KDIVE_MCP_TOOL_GATEWAY` now defaults on.

## Consequences

- Agent and operator clients can use different discovery profiles over one MCP transport and one
  authz model.
- The gateway becomes a profile behavior, not a global deployment-wide default that breaks
  `kdivectl`.
- Operator direct-tool access is not a privilege escalation: every listed tool still enforces its
  own RBAC and destructive-operation gates at call time.
- Search metadata becomes part of the discoverability contract for future tool consolidations.

## Rejected alternatives

- **A caller-selected profile header.** It is easy to spoof and would turn exposure into a
  caller-controlled policy input.
- **A second operator transport.** It duplicates authentication, authorization, audit, and schema
  generation for no new capability.
- **Keeping the single global gateway switch.** It cannot make normal agents compact while keeping
  `kdivectl` direct on the same deployment.
- **Compatibility aliases for removed tools.** The project is pre-release and follows
  replace-don't-deprecate; discoverability belongs in schema-aware search, not hidden wrappers.
