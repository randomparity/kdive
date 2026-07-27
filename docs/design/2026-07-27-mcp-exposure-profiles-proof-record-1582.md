# Proof record — agent/operator MCP exposure profiles + gateway default flip (#1581, #1582)

Date: 2026-07-27
Issues: #1581 (profiles), #1582 (default flip) · Epic: #1576
ADRs: [0456](../adr/0456-agent-operator-mcp-exposure-profiles.md) (exposure profiles),
[0268](../adr/0268-tool-gateway-dispatcher.md) §7 (empirical verification gate),
[0089](../adr/0089-operator-cli-mcp-client.md) decision 5 (trusted `kdivectl` client identity)

## What this proves

ADR-0268 §7 and ADR-0456 §6 both gate the `KDIVE_MCP_TOOL_GATEWAY` default flip on real-client
evidence, not unit tests. ADR-0267's gateway was reverted precisely because it shipped default-on
without a real-client run. This record is that evidence, collected against a live local stack with
the profile code in place:

1. a **real Claude Code cold start** that discovers and invokes a non-listed tool through the
   gateway, and recovers from a validation failure and an authorization denial; and
2. a **second real Claude Code cold start** that drives a full unit of work — allocation →
   provision → poll a job to a terminal state — where the polling tool is reached only through the
   gateway; and
3. a **`kdivectl` operator-profile proof** that read-only, mutating, and destructive direct tools
   still classify correctly from live `list_tools` annotations, with an agent-token negative
   control showing the fail-closed failure mode the profile prevents.

## Environment

- Host: x86_64 Fedora, Linux 7.1.3, libvirt `qemu:///system` reachable, `/dev/kvm` present.
- Stack: `KDIVE_WORKER_AS_ROOT=0 scripts/live-stack/up.sh --skip-libvirt --skip-obs` —
  Postgres/MinIO/mock-OIDC backends plus host server/reconciler/worker at
  `http://127.0.0.1:8000/mcp`. Server build stamp `0.4.1-dev+g22e503ddd` running the working tree
  under test.
- `KDIVE_MCP_TOOL_GATEWAY` **unset** for every run below, so each result exercises the new default,
  not an explicit opt-in.
- Real client: Claude Code `2.1.220`, run headless (`claude -p`) from an empty working directory
  with `--strict-mcp-config` and every non-MCP tool denied, so the agent had the kdive MCP server
  and nothing else — no repository, no docs, no prior knowledge of kdive.
- Tokens minted from the local mock issuer, differing only in `azp`:

  | token | `azp` | roles | platform roles |
  |-------|-------|-------|----------------|
  | agent | `kdive-agent` | `demo: contributor` | none |
  | agent-with-platform-role | `kdive-agent` | `demo: admin` | `platform_operator` |
  | operator CLI | `kdivectl` | `demo: admin` | `platform_operator` |

## 1. Profile selection — ground truth

Raw MCP `list_tools` over the wire, no agent in the loop:

| token | tools listed | annotated |
|-------|--------------|-----------|
| agent (contributor) | **9** — exactly `CORE_TOOLS` | 9 |
| agent **with `platform_operator`** | **9** — exactly `CORE_TOOLS` | 9 |
| operator CLI (`azp=kdivectl`) | **125** — the full RBAC-visible catalog | 125 |

The middle row is the escalation-shaped case ADR-0456 §1 guards: holding a platform role does not
buy a caller the operator profile. Only the verified `kdivectl` OIDC client id does.

## 2. Agent cold start — discovery, gateway reach, failure recovery

One `claude -p` run, 15 turns. The client bound exactly nine kdive tools:
`allocations.request`, `allocations.wait`, `runs.create`, `runs.get`, `runs.list`,
`session.whoami`, `systems.provision`, `tools.invoke`, `tools.search`.

- **Reached a non-listed tool.** `tools.search("list available kernel images for this deployment")`
  returned `images.list` with its full input schema, annotations (`readOnlyHint: true`) and
  maturity; `tools.invoke(name="images.list", arguments={})` then returned 10 images. Discovery to
  execution used only server-provided affordances — the server `instructions` block named the
  gateway, and search returned enough schema to call the tool without further lookup.
- **Validation failure recovered in one round trip.** A deliberate bogus argument returned
  `error_category: configuration_error`, `retryable: false`,
  `detail: "Arguments for 'images.list' failed schema validation."`. The agent re-read the schema
  search had already handed it and the corrected call succeeded.
- **Authorization denial was clean and terminal-for-the-capability, not a dead end.**
  `ops.diagnostics` (requires `plat-operator`; this token holds no platform role) returned
  `error_category: authorization_denied`, `retryable: false`, `detail: "access denied"` — the same
  envelope shape as a success, and the session stayed usable.

The agent's own summary: it started with 9 bound tools out of 140 registered and reached a
non-listed capability end to end "using only server-provided affordances. No guessing, no
out-of-band documentation."

## 3. Agent cold start — full lifecycle, job polled to terminal

A second `claude -p` run, 18 turns, 134 s wall clock, satisfying ADR-0268 §7's "discover → invoke
through the gateway → poll one job to terminal" flow.

- `allocations.request` (listed) granted allocation `ee417dea…` on shape `small`, admitted
  immediately.
- `systems.provision` (listed) returned provision job `5aa98131…` for system `9dd89744…`.
- **Polling went through the gateway.** The agent found `jobs.wait` — not in its listed set — via
  `tools.search("poll background job status until terminal")` and called it as
  `tools.invoke(name="jobs.wait", …)`. Terminal state on the first 30 s wait:

  ```json
  {"object_id":"5aa98131-a81d-41ed-b28a-b31c9bf4b1e6","status":"succeeded",
   "suggested_next_actions":["jobs.get"],"refs":{"result":"9dd89744-203d-4579-97a2-713a49bd0693"},
   "error_category":null,"retryable":null,"detail":null,"data":{"kind":"provision"},"items":[]}
  ```

  A real guest booted: `systems.get` reported the system `ready`, `accel: "kvm"`, 1 vCPU / 1024 MB
  / 10 GB. Torn down and the allocation released after the run (`torn_down` / `released`, no
  `kdive-*` libvirt domains left behind).
- Seven further non-listed tools were reached the same way — `shapes.list`,
  `resources.availability`, `images.list`, `images.describe`, `systems.profile_examples`,
  `jobs.get`, `systems.get`.

## 4. Operator profile — `kdivectl` keeps live annotations across all three safety tiers

`kdivectl tool call` resolves a tool's tier from the **live** server annotations, never the
committed verb artifact, and `UNKNOWN` (which includes "absent from `list_tools`") is fail-closed
at every tier. That makes the operator profile load-bearing rather than cosmetic: a `kdivectl`
clipped to `CORE_TOOLS` would classify every non-core verb `UNKNOWN` and refuse it.

Same server, same default-on gateway, tokens differing only in `azp`:

| call | operator token (`azp=kdivectl`) | agent token (`azp=kdive-agent`) |
|------|--------------------------------|--------------------------------|
| `tool call images.list` (read-only) | executes, returns the catalog | `'images.list' is not positively classified … unreachable via 'tool call'` |
| `tool call images.publish` (mutating) | `'images.publish' is mutating; pass --allow-mutating to call it` | — |
| `tool call systems.teardown --allow-mutating` (destructive) | `'systems.teardown' is destructive; pass --allow-destructive to call it` | `not positively classified … unreachable` |
| `tool call session.whoami` (core) | executes | executes |

All three tiers classify correctly under the operator profile, and the agent-token column shows the
failure mode is fail-closed refusal, not silent over-permission. Destructive teardown with
`--allow-destructive --yes` then ran cleanly, which is how the proof VM was reclaimed.

Note: hand-curated `kdivectl` verbs (e.g. `kdivectl images list`) call their tool directly and do
not consult live annotations, so they keep working under either profile. The live-annotation
dependency is specific to `kdivectl tool call` and the schema-generated dispatch path.

## Findings filed, not fixed here

The cold-start runs surfaced two agent-experience defects that are out of scope for #1581/#1582 and
neither blocks the flip:

1. An `authorization_denied`, `retryable: false` envelope still returned
   `suggested_next_actions: ["ops.diagnostics"]` — pointing back at the tool that just denied the
   caller. A naive agent could loop on that affordance.
2. `tools.search` with `limit: 10` returned ~54 KB of inline schemas in one result, and a
   `namespace` query for a plane the token cannot reach returns an empty set that is
   indistinguishable from "no such namespace". Both are ADR-0456 §3-conformant (search is
   RBAC-scoped and returns full schemas by design) but expensive and ambiguous in practice.

## Verdict

Both ADR-0456 §6 gates and the ADR-0268 §7 gate are satisfied by real-client runs recorded above.
The agent profile defaults to the compact gateway surface; the operator CLI keeps direct exposure
with live annotations. The flip is proven, not assumed.
