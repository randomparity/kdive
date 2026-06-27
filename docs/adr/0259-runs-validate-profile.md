# ADR-0259: `runs.validate_profile` — a no-insert build-profile check (#839)

- Status: Accepted
- Date: 2026-06-26

## Context

`runs.create`'s `build_profile` is a nested discriminated union: the server lane
(`source="server"`: a `kernel_source_ref` that is either a warm-tree string or a
`{git:{remote,ref}}` object, plus optional config/profile refs) and the external lane
(`source="external"`: the discriminator alone). An agent that hand-assembles a profile has no
cheap way to confirm it is well-formed and host-compatible before committing to a Run:

- **Structural feedback is boundary-shaped, not envelope-shaped.** `runs.create` types its
  `build_profile` parameter as `ExternalBuildProfile | ServerBuildProfile`
  (`src/kdive/mcp/tools/lifecycle/runs/registrar.py:148-149`), so a malformed document is
  rejected by Pydantic's *union discrimination* at the FastMCP boundary — it attempts both
  variants and emits merged, source-ambiguous errors — rather than by
  `BuildProfile.parse`, whose `source`-dispatched, redacted `configuration_error`
  (`src/kdive/profiles/build.py`) is the project's sanctioned, agent-legible failure shape.
- **The semantic check sits behind the full precondition scaffold.** Build-host ↔
  kernel-source compatibility (`check_source_kind_compatibility`, the single source of truth
  for the host-kind/source-kind matrix, ADR-0099 §5) runs inside `runs.create` only at
  `_compat_block_response` (`src/kdive/services/runs/admission.py:500`), which is reached only
  after the caller has a funded project, an **open Investigation**, and (for a bound Run) a
  **ready System with an active Allocation**. The compat check does precede the Run insert
  (`admission.py:513`), so a mismatch does not itself create a Run — but reaching it at all
  costs the entire scaffold, and any malformed document fails earlier at the boundary.
- **`runs.profile_examples` (ADR-0160) does not validate.** It emits a near-complete example
  to edit ("not buildable as-is"); it does not check caller-supplied input.

So the only authoritative feedback on a hand-assembled profile is to drive `runs.create` with
real preconditions — expensive for a compatibility question, and boundary-shaped for a
structural one. The issue (`status:needs-design`) asks for a validate-only path and flags the
shape decision — standalone tool vs a flag on `runs.create` — as worth settling first.

## Decision

Add `runs.validate_profile(build_profile) -> ToolResponse`: a **standalone read-only,
auth-only, pool-backed** MCP tool on the `runs.*` registrar
(`@app.tool(name="runs.validate_profile", annotations=read_only, meta={"maturity":
"implemented"})`), modeled on its read-only sibling `runs.profile_examples`.

**Raw-document parameter.** `build_profile` is typed as the raw `BuildProfileInput`
(`Mapping[str, object]` — the same boundary type `runs.create` already uses for
`expected_boot_failure`), **not** the parsed union. This is the load-bearing choice: a union
type would re-create the boundary-discrimination problem and short-circuit the handler. With
the raw type, the unparsed document reaches the handler, and the handler — not FastMCP —
produces the verdict via `BuildProfile.parse`, yielding the typed envelope the tool exists to
provide.

**Algorithm** (no Run insert, no capacity, no Investigation/System/Allocation, no audit):

1. `current_context()` — token presence (ADR-0117 auth-only).
2. `BuildProfile.parse(build_profile)`; on `CategorizedError` (always `configuration_error`,
   redacted of submitted values per ADR-0029) return `failure_from_error` with
   `suggested_next_actions=["runs.profile_examples"]`.
3. External lane → valid (no host/source-tree fields to check).
4. Server lane → resolve `name = profile.build_host or "worker-local"`,
   `host = get_by_name(conn, name)`.
5. `host is None` → **not rejected** (matches `_compat_block_response`'s absent-host allow; the
   host may be registered before build), recording `build_host_registered=false`. `host`
   present → `check_source_kind_compatibility(...)`; on raise return the same
   `configuration_error` a create-time mismatch yields.
6. Valid → `ToolResponse.success(OBJECT_ID, "valid", data=…,
   suggested_next_actions=["runs.create"])`, where `data` carries `source`, the normalized
   `profile = dump_build_profile(parsed)` (paste-ready for `runs.create`), and — server lane —
   `build_host`, `build_host_registered`, `host_kind`, `source_kind`.

`OBJECT_ID` is the stable literal `"profile-validation"`. The external lane and a parse failure
never open a DB connection.

**Parity is enforced, not asserted.** `validate_profile` consumes the same
`BuildProfile.parse` and `check_source_kind_compatibility` primitives as `runs.create`, and
replicates the two create-time framing lines (the `"worker-local"` default and the absent-host
allow). A test pins that its compat verdict equals `_compat_block_response`'s for a matrix of
profiles, so the two surfaces cannot drift.

No schema, migration, RBAC role, or config setting. The MCP surface change is purely additive.

## Consequences

- An agent can confirm a hand-assembled profile parses and is host-compatible for the cost of
  one read call — no funded project, Investigation, System, or Allocation, and no durable Run
  row or capacity lease — and gets the project's typed `configuration_error` envelope (redacted
  field locations) instead of a boundary-level Pydantic union error.
- A `valid` verdict is parse-and-compat only, like `profile_examples`: it does not assert the
  source tree exists, the config resolves, the kernel builds, or capacity is free. The
  `build_host_registered=false` field discloses when the compat check was skipped for an
  unregistered host, so the verdict's scope is never silently overstated.
- `validate_profile` matches `runs.create`'s **create-time** verdict exactly (shared
  primitives + pinned parity test), so it never rejects a pairing create would accept nor
  accepts one create would reject. It deliberately does not check host availability
  (enabled/reachable/at-capacity), which create also defers to `runs.build`.
- New read tool → it must be added to the generated tool reference
  (`docs/guide/reference/runs.md`, CI `docs-check`) regenerated via `just docs`. It is
  auth-only with no curated CLI verb, so — like `runs.profile_examples` — it is not added to
  `READ_TOOLS` or the curated-verb guards.

## Considered & rejected

- **A `validate_only: true` flag on `runs.create`.** `runs.create` is annotated `mutating()`;
  a flag that makes it conditionally read-only corrupts a load-bearing safety annotation that
  agents and the read-only CLI passthrough (ADR-0089) rely on. It would also need its
  create-only parameters (`investigation_id`, `system_id`, `idempotency_key`, …) made
  meaningless-when-set, and would give `runs.create` two response contracts. A standalone
  read-only tool keeps one tool one contract and sits beside the existing read-only
  `runs.profile_examples`.
- **Typing `build_profile` as the parsed union** (as `runs.create` does). Re-creates the exact
  boundary-discrimination problem the tool exists to fix: the FastMCP layer would reject a
  malformed document before the handler runs, so the typed envelope would never be produced.
  The raw `Mapping` type is required.
- **Checking host availability (enabled/reachable/at-capacity).** Would make `validate_profile`
  stricter than `runs.create`'s create-time check and could reject a pairing create accepts
  (e.g. a host briefly unreachable at validate time). Availability is `runs.build`'s concern
  (`resolve_and_admit`); validate mirrors the create-time verdict only.
- **Rejecting an unregistered named `build_host`.** `runs.create` allows it (the host may be
  registered between create and build); rejecting here would diverge from the create verdict.
  Disclosed via `build_host_registered=false` instead.
- **Reusing `_compat_block_response` directly.** It returns an admission-internal
  `RunCreateError` tied to the create object-id semantics. `validate_profile` calls the shared
  `check_source_kind_compatibility` primitive instead and pins parity with a test, keeping the
  read tool independent of admission internals.
- **A curated `kdivectl runs validate-profile` verb.** Out of scope and inconsistent with the
  read-only sibling `runs.profile_examples`, which has none; the generic read-only passthrough
  already reaches it.
- **Auditing the validation call.** It changes no state and reads only the public build-host
  projection `profile_examples` already exposes auth-only; an audit row per validation is
  noise (matches `profile_examples`, ADR-0160).
