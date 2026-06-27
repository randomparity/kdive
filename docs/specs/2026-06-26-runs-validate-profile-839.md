# Spec: `runs.validate_profile` — a no-insert build-profile check (#839)

- Date: 2026-06-26
- ADR: [ADR-0259](../adr/0259-runs-validate-profile.md)
- Status: Draft

## Problem

`runs.create`'s `build_profile` is a nested discriminated union (server vs external lane;
warm-tree string vs `{git:{remote,ref}}` object; nested config/profile refs). An agent that
wants to know whether a profile it assembled is well-formed and compatible with its target
build host has no cheap way to ask:

- The structural shape is validated at the FastMCP boundary because `runs.create` types
  `build_profile` as `ExternalBuildProfile | ServerBuildProfile`
  (`runs/registrar.py:148-149`). A malformed document is rejected by Pydantic's union
  discrimination — which tries both variants and emits merged, source-ambiguous errors —
  rather than by `BuildProfile.parse`, whose `source`-dispatched, redacted
  `configuration_error` envelope (`profiles/build.py`) is the project's sanctioned, agent-
  legible feedback shape.
- The semantic check — build-host ↔ kernel-source compatibility
  (`check_source_kind_compatibility`, the shared single source of truth for the
  host-kind/source-kind matrix) — runs inside `runs.create` only after the caller has a
  funded project, an **open Investigation**, and (for a bound Run) a **ready System with an
  active Allocation**. Reaching it (`admission.py:500`, `_compat_block_response`, before the
  Run insert at `admission.py:513`) costs the whole precondition scaffold. There is no way to
  ask "is this profile compatible with host X?" without standing that scaffold up.
- `runs.profile_examples` (ADR-0160) hands over a near-complete example profile to edit, but
  explicitly **does not validate** caller-supplied input ("not buildable as-is").

So the only way to get authoritative feedback on a hand-assembled profile is to drive
`runs.create` with real preconditions — and a structurally-invalid profile yields a
boundary-level Pydantic error rather than the typed envelope, while a compatibility mismatch
is only discoverable after building the precondition context.

## Goal

Add `runs.validate_profile(build_profile)`: a read-only, auth-only MCP tool that runs the same
parse + compatibility checks `runs.create` runs, over the **raw** profile document, and
returns the project's typed `ToolResponse` envelope — **without** inserting a Run, consuming
capacity, requiring an Investigation/System/Allocation, or writing an audit record.

## Non-goals

- **No `validate_only` flag on `runs.create`.** See ADR-0259 rejected alternatives.
- No new schema, migration, RBAC role, or config setting.
- No buildability guarantee. Like `runs.profile_examples`, a `valid` verdict means the
  document parses and (for a registered named host) is source-kind compatible — not that the
  source tree exists, the config resolves, the kernel compiles, or capacity is free. Those are
  resolved later at `runs.build`/`runs.complete_build`.
- No host *availability* check (enabled/reachable/at-capacity). `runs.create`'s create-time
  compat check (`_compat_block_response`) does not check those either — they are re-validated
  at `runs.build` (`resolve_and_admit`). `validate_profile` matches the create-time verdict
  exactly, so it never rejects a pairing `runs.create` would accept, nor accepts one it would
  reject.
- No CLI verb. The read-only sibling `runs.profile_examples` has no curated `kdivectl` verb;
  `validate_profile` follows it (still reachable through the generic read-only passthrough).

## Decision summary (see ADR-0259)

A **standalone read-only tool**, not a mode flag on the mutating `runs.create`. The
`build_profile` parameter is typed as the **raw document** (`BuildProfileInput`,
i.e. `Mapping[str, object]` — the same boundary type `runs.create` already uses for
`expected_boot_failure`), *not* the parsed `ExternalBuildProfile | ServerBuildProfile` union,
so the unparsed document reaches the handler and the handler — not the FastMCP boundary —
produces the verdict via `BuildProfile.parse`.

## Behavior

### Auth posture

Auth-only (ADR-0117/0160), identical to `runs.profile_examples`: a valid token gates the
transport as defence-in-depth (`current_context()`), but there is no platform/project gate and
no audit. The tool reads the same public `build_hosts` projection `profile_examples` already
exposes auth-only, and grants no new visibility — it only validates caller-supplied input.

### Algorithm

1. `current_context()` — enforce token presence.
2. `parsed = BuildProfile.parse(build_profile)`. On `CategorizedError` (always
   `CONFIGURATION_ERROR`), return `ToolResponse.failure_from_error(OBJECT_ID, exc,
   suggested_next_actions=["runs.profile_examples"])`. The error `details` carry field
   locations/types/messages but never submitted values (the ADR-0029 redaction guarantee, plus
   `safe_error_details`).
3. If `parsed` is an `ExternalBuildProfile`: it is valid (the external lane has no host or
   source-tree fields to check). Go to step 6.
4. `parsed` is a `ServerBuildProfile`: `name = parsed.build_host or "worker-local"`;
   `host = await get_by_name(conn, name)`.
5. Compatibility:
   - `host is None` → **not rejected** (the host may be registered between validate and build;
     this matches `_compat_block_response`'s absent-host allow). Record
     `build_host_registered = False`.
   - `host` present → `check_source_kind_compatibility(host_kind=host.kind,
     is_git=is_git_source(parsed), build_host=name)`. On `CategorizedError`, return
     `failure_from_error(OBJECT_ID, exc, suggested_next_actions=["runs.profile_examples"])`
     (the same `configuration_error` a create-time mismatch yields). Record
     `build_host_registered = True`, `host_kind = host.kind.value`.
6. Success: `ToolResponse.success(OBJECT_ID, "valid", data=<below>,
   suggested_next_actions=["runs.create"])`.

`OBJECT_ID` is a stable literal `"profile-validation"` (no Run id exists). A single
connection from the pool is used for the optional `get_by_name`; the external lane and a
parse failure never touch the DB.

### Success `data`

- `source`: `"server"` or `"external"`.
- `profile`: `dump_build_profile(parsed)` — the normalized, canonical document (defaults
  applied, e.g. an omitted `source` resolved to `"server"`), ready to paste verbatim into
  `runs.create`.
- Server lane only:
  - `build_host`: the resolved name (`parsed.build_host or "worker-local"`).
  - `build_host_registered`: `bool` — `false` means the compat check was **skipped** because no
    such host is registered (the agent is told the verdict is parse-only for that host).
  - `host_kind`: the host's transport kind value, or `null` when `build_host_registered` is
    `false`.
  - `source_kind`: `"git"` or `"warm-tree"` (the provenance the document selected).

### Failure `data`

The redacted `CategorizedError.details` (field `errors` list for a parse failure; `build_host`
+ `host_kind` for a compat mismatch), `error_category = "configuration_error"`,
`suggested_next_actions = ["runs.profile_examples"]`.

## Edge & error cases (each gets a test)

| Input | Outcome |
|-------|---------|
| Valid external `{"schema_version":1,"source":"external"}` | `valid`, `source=external`, no host fields |
| Valid server warm-tree against the seeded `worker-local` (local) host | `valid`, `source_kind=warm-tree`, `build_host_registered=true`, `host_kind=local` |
| Valid server `{git:{remote,ref}}` against `worker-local` | `valid`, `source_kind=git` |
| Server warm-tree naming an **ssh** host | `configuration_error` (compat: remote host requires git) |
| Server profile naming an **unregistered** `build_host` | `valid`, `build_host_registered=false`, `host_kind=null` (compat skipped) |
| Omitted `source` (defaults to server) with a valid warm-tree ref | `valid`, normalized `profile.source="server"` |
| Unknown `source` value | `configuration_error` ("unknown build source") |
| Unknown/extra field (`extra=forbid`) | `configuration_error` |
| External profile carrying server-only fields (e.g. `kernel_source_ref`) | `configuration_error` |
| Bare-URL `kernel_source_ref` (`https://…`) | `configuration_error` (ADR-0242 bare-URL guard); message names only the scheme, never the value |
| Empty/whitespace required string (`kernel_source_ref:""`) | `configuration_error` |
| `wrong_type` for `schema_version` (e.g. `"1"`/`2`) | `configuration_error` |

The handler's non-`Mapping` defensive branch in `BuildProfile.parse` is unreachable through the
transport (the `Mapping[str, object]` boundary type rejects a non-object JSON `build_profile`
first); the parse path is still driven directly in a unit test.

## Parity invariant (test)

`validate_profile`'s compatibility verdict must equal `runs.create`'s create-time verdict for
the same `(build_profile, build_hosts)`. A test pins this by asserting that, for a matrix of
profiles, `validate_profile`'s pass/fail agrees with `_compat_block_response` (both consume the
shared `check_source_kind_compatibility`), so the two surfaces cannot drift.

## Guardrail impact

- New tool → regenerate the tool reference (`docs/guide/reference/runs.md`) via `just docs`;
  `docs-check` gates it.
- Auth-only, no curated CLI verb → not added to `READ_TOOLS`/CLI-verb guards (matches
  `runs.profile_examples`).
- `runs.validate_profile` registered on the `runs.*` registrar with
  `annotations=_docmeta.read_only()`, `meta={"maturity": "implemented"}`.
