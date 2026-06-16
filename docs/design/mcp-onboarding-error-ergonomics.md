# MCP onboarding & error-ergonomics findings

- **Epic:** [#449](https://github.com/randomparity/kdive/issues/449)
- **Work items:** [#450](https://github.com/randomparity/kdive/issues/450) (B, error detail) ·
  [#451](https://github.com/randomparity/kdive/issues/451) (A, profile discoverability) ·
  [#452](https://github.com/randomparity/kdive/issues/452) (C, transport bound) ·
  [#453](https://github.com/randomparity/kdive/issues/453) (D, diagnostics reachability)
- **ADRs:** [`0123`](../adr/0123-tool-error-detail-surfacing.md) ·
  [`0124`](../adr/0124-provisioning-profile-discoverability.md) ·
  [`0125`](../adr/0125-diagnostics-host-reachability.md) ·
  [`0126`](../adr/0126-synchronous-tool-transport-bound.md)
- **Status:** Draft

## Problem

A first-time agent driving the MCP surface end-to-end (read → accounting → allocation →
provision → run → boot → crash → debug) got through every read/accounting/allocation/audit
tool cleanly but hit a hard wall at `systems.provision` / `systems.define` and could not learn
its way past it. The wall and its three smaller siblings share one theme: **the server holds
the information the caller needs and discards it before the wire.**

Four findings, in priority order:

1. **The provisioning `profile` is undiscoverable.** The MCP tools type the parameter as
   `ProvisioningProfileInput = Mapping[str, object]` (`src/kdive/profiles/types.py:9`), so
   FastMCP advertises a freeform `additionalProperties: true` blob. The real schema — a
   fully-structured Pydantic `ProvisioningProfile` with a discriminated `rootfs`, required
   fields, and a `boot_method`↔provider pairing rule — lives three layers down
   (`src/kdive/profiles/provisioning.py`) and is invisible to the client. The reporter tried
   ten evidence-based profile shapes and every one was rejected identically.

2. **Error envelopes carry no human-readable reason.** The envelope (`ToolResponse`,
   `src/kdive/mcp/responses.py`) has `error_category` but no `detail`/`message` field, and the
   structured validation errors that `ProvisioningProfile.parse()` *does* attach
   (`details["errors"]`, `provisioning.py:283-287`) are dropped by `_safe_error_details`
   (`admission.py:120-129`, which filters `data` to scalars — and `errors` is a list).
   `CategorizedError`'s message reaches `Exception.__init__` but is never threaded onto the
   envelope. The reporter's `configuration_error` came back with empty `data` and no message.
   Fixing this finding alone would have unblocked the reporter even without finding 1.

3. **A long `systems.provision` dropped the socket** ("The socket connection was closed
   unexpectedly") instead of returning an envelope; a retry succeeded. `systems.provision`
   enqueues a job and returns fast, so the drop is most likely a synchronous DB/libvirt call
   blocking the async event loop in the request path, with no dispatch-level timeout to convert
   a stall into a `transport_failure` envelope.

4. **`ops.diagnostics` does not probe host reachability.** For a remote-libvirt host it ran
   only the server-vantage `secret_ref` check (`diagnostics/service.py:204-235`); the
   `ProviderTlsCheck`/`GdbstubAclCheck` checks exist (`diagnostics/checks.py:260-362`) but are
   not wired into the default factory (deferred to an "egress-probe wave"), and there is no
   `qemu+tls://` connection probe at all. So diagnostics could not tell the reporter whether
   the wall was a bad profile or an unreachable host.

## Acceptance criteria

1. A new agent can discover a valid provisioning profile for any configured provider from the
   MCP surface alone — no source reading, no external docs (findings 1).
2. Every rejected tool call returns a one-line human-readable `detail` and, where the failure
   is a structured validation error, the machine-readable field paths — without weakening the
   no-leak invariant for `authorization_denied`/`not_found` (finding 2).
3. A synchronous tool that stalls returns a `transport_failure` envelope, not a dropped socket;
   the request path holds no blocking call on the event loop (finding 3).
4. `ops.diagnostics` reports per-host reachability for a remote-libvirt provider, distinguishing
   "unreachable host" from "bad config" (finding 4).

## What already exists (verified in source)

| Building block | Where |
|---|---|
| `ProvisioningProfile` Pydantic model (discriminated `rootfs`, provider sections, pairing rule) | `src/kdive/profiles/provisioning.py` |
| `ProvisioningProfile.parse()` maps `ValidationError` → `CONFIGURATION_ERROR` with `details["errors"]` | `src/kdive/profiles/provisioning.py:260-287` |
| Freeform param alias `ProvisioningProfileInput = Mapping[str, object]` | `src/kdive/profiles/types.py:9` |
| `systems.define` / `systems.provision` registration | `src/kdive/mcp/tools/lifecycle/systems/registrar.py:68-130` |
| `ToolResponse` envelope (`error_category`, `data`, `suggested_next_actions`; **no `detail`**) | `src/kdive/mcp/responses.py:89-226` |
| `ToolResponse.failure_from_error` (extracts `exc.details`, drops `str(exc)`) | `src/kdive/mcp/responses.py:194-210` |
| `_safe_error_details` (scalar-only filter; drops the `errors` list) | `src/kdive/mcp/responses.py` + `src/kdive/services/systems/admission.py:120-129` |
| `CategorizedError(message, category, details)` | `src/kdive/domain/errors.py:57-82` |
| Read-only discovery-tool precedent (`projects.list` whoami: auth-only, no gate, no audit) | ADR-0117, `src/kdive/mcp/tools/.../projects.py` |
| Flat output-schema sweep (FastMCP 3.4.0 client choked on recursive `$ref` **outputs**) | ADR-0113, `build_app` |
| Diagnostics service factory (wires only `secret_ref`) | `src/kdive/diagnostics/service.py:204-235` |
| Unwired `ProviderTlsCheck` / `GdbstubAclCheck` | `src/kdive/diagnostics/checks.py:260-362` |
| `remote_connection()` (`qemu+tls://` open + cleanup) and `conn.getInfo()` | `src/kdive/providers/remote_libvirt/transport.py:54,146-181` |
| `SshBuildHostProber` (timeout + `asyncio.to_thread` reachability pattern) | `src/kdive/providers/shared/build_host/reachability.py:44-93` |
| `systems.toml` inventory (`[[image]]`, `[[remote_libvirt]]`) feeding profile references | `systems.toml`, `src/kdive/providers/remote_libvirt/config.py:178-210` |

## Design

The four work items are ordered so the shared seam lands first. **Work item B (error detail)
is the foundation** — A's typed-param error path and C's transport envelope both depend on it.

### Work item B — Tool-error detail surfacing (ADR-0123, finding 2)

Add a `detail` carrier to the envelope and stop discarding structured validation errors. One
shared seam, all tools benefit.

- **Envelope field:** `ToolResponse` gains `detail: str | None = None`. Populated from
  `str(exc)` (the `CategorizedError` message) in `failure_from_error` and exposed as a `detail=`
  kwarg on `failure()`. Advertised schema stays the flat `{"type": "object"}` (ADR-0113); this
  is an additive field on the wire payload, not an output-schema change.
- **Structured errors survive:** `_safe_error_details` is widened to preserve one reserved
  nested key — `errors: list[{loc, msg, type}]` (exactly the shape `parse()` already produces
  via `exc.errors(include_url=False, include_input=False, include_context=False)`) — bounded to
  the first 20 entries, each entry sanitized to scalars. All other `data` keys keep the
  scalar-only rule. The `input` value is already stripped at the throw site, so no caller input
  echoes back.
- **Both seams fixed:** the `ToolResponse` path (`responses.py`) **and** the admission service's
  private `_failure_from_error`/`_safe_error_details` (`admission.py`), which builds an
  `AdmissionFailure` later mapped to `ToolResponse`. `AdmissionFailure` gains a `detail` field
  threaded through the `provision.py` mapper. (The duplicated `_safe_error_details` is
  consolidated to one helper.)
- **No-leak guard (load-bearing):** `detail` is populated for diagnostic categories
  (`configuration_error`, `missing_dependency`, `build_failure`, …) but **stays generic** for
  `authorization_denied` and the by-id `not_found` no-leak path (ADR-0097/0098). The membership
  denial envelope (`ProjectMembershipDenied`, ADR-0098) is constructed without a leaky detail —
  a regression test asserts no resource name appears in a non-member denial's `detail`.

### Work item A — Provisioning-profile discoverability (ADR-0124, finding 1)

Two cooperating changes; the discovery tool is the guaranteed-working half.

- **Typed parameter:** `systems.define`/`systems.provision` type `profile` as
  `ProvisioningProfile` so FastMCP advertises its JSON schema (required fields, the discriminated
  `rootfs`, the provider sections). Because FastMCP validates a typed model **at the boundary**,
  before the tool body, a bad profile would otherwise raise FastMCP's own `ValidationError` and
  bypass our envelope. A dispatch-boundary conversion (reusing work item B's surfacing) catches
  `pydantic.ValidationError` raised during input binding and returns the standard
  `configuration_error` envelope with `detail` + `errors`. This unifies findings 1 and 2 at the
  input boundary.
- **Spike (gates the typed-param half):** confirm the FastMCP 3.4.0 client renders a
  `$defs`/`discriminator` **input** schema cleanly. ADR-0113 flattened *output* schemas because
  the client's per-call `TypeAdapter` choked on recursive `$ref`; inputs are validated
  server-side and only displayed, and `ProvisioningProfile` is not self-recursive — but the
  spike verifies the client neither errors nor renders an unusable blob. If it chokes, the
  typed-param half falls back to a hand-authored flattened input schema (still far richer than
  `additionalProperties: true`); the discovery tool below ships regardless.
- **Discovery tool `systems.profile_examples`** (modeled on `projects.list`, ADR-0117):
  read-only, auth-only, no project gate, no audit. Returns one item per configured provider with
  a ready-to-edit example profile dict, populated with real reference names drawn from the
  `systems.toml` inventory (e.g. the `base_image_volume` for remote-libvirt, catalog image names
  for local-libvirt). Chains into `systems.define` / `allocations.request` via
  `suggested_next_actions`. This is the always-works answer to "what is a valid profile here?"

### Work item D — Diagnostics host reachability (ADR-0125, finding 4)

- Wire the existing `ProviderTlsCheck`/`GdbstubAclCheck` into the default diagnostics service
  factory (closing the deferred "egress-probe wave" gap).
- Add a remote-libvirt **reachability check**: open `remote_connection()` and call
  `conn.getInfo()` under a bounded per-check timeout, reusing the `SshBuildHostProber` pattern
  (`asyncio.to_thread` + timeout). Report per-host `pass`/`fail`/`error` with the connection
  failure category (`transport_failure` vs `configuration_error`), so a caller can tell
  "host unreachable" from "bad config." Gated like the other diagnostics checks (ADR-0091).

### Work item C — Synchronous-tool transport bound (ADR-0126, finding 3)

- **Audit + offload:** find the synchronous blocking call(s) in the `systems.provision` request
  path (DB lock acquisition / libvirt call during admission) and offload to `asyncio.to_thread`
  so the event loop is never blocked while one request runs.
- **Dispatch-boundary timeout:** wrap synchronous tool bodies with an execution-time bound; on
  timeout (or an otherwise-uncaught transport-level failure), return a `transport_failure`
  envelope (with work item B's `detail`) instead of letting the socket drop.
- **Spike (gates the bound's threshold):** reproduce the stall to confirm the blocking call and
  set the timeout above the legitimate worst case.

## Failure modes and edges

| Condition | Result |
|---|---|
| Bad profile via typed param | Boundary `ValidationError` → `configuration_error` envelope + `detail` + `errors` field-paths |
| Bad profile, FastMCP client can't render typed input schema | Spike fallback: flattened input schema; `systems.profile_examples` still serves a valid example |
| `authorization_denied` / non-member denial | Generic `detail` only; no resource name leaks (no-leak regression test) |
| `not_found` by id | Generic `detail`; ADR-0097 no-leak path untouched |
| Structured error with >20 sub-errors | First 20 surfaced; bounded so the envelope stays small |
| `systems.provision` stalls on a blocking call | `transport_failure` envelope, not a dropped socket |
| Remote-libvirt host down | `ops.diagnostics` reachability check reports `fail` + `transport_failure` |
| Remote-libvirt misconfigured URI/cert | reachability check reports `error` + `configuration_error` |
| `systems.profile_examples` with no inventory | Returns generic per-provider examples (placeholder reference names) + a note |

## Test plan (behavior, not implementation)

- **B:** a rejected `configuration_error` carries a non-empty `detail` and an `errors` list with
  field paths; an `authorization_denied` / non-member denial carries a generic `detail` with no
  resource name (no-leak guard); the `errors` list is bounded at 20 and input values never echo.
- **A:** the advertised `systems.define` input schema is no longer `additionalProperties: true`
  (or, on spike-fallback, is the flattened schema); a malformed profile returns the
  `configuration_error` envelope (not a raw FastMCP error); `systems.profile_examples` returns a
  valid example per provider whose `base_image_volume`/catalog name matches the `systems.toml`
  inventory, and the example round-trips through `systems.define` without a `configuration_error`.
- **D:** the default diagnostics factory now includes the TLS/ACL checks; a reachable host
  reports `pass`, an unreachable host reports `fail` + `transport_failure`, a bad URI reports
  `error` + `configuration_error`.
- **C:** a tool body that exceeds the bound returns a `transport_failure` envelope rather than
  raising/dropping; the provision request path runs no sync blocking call on the event loop
  (asserted by an injected slow DB/libvirt double not stalling a concurrent request).
- **Wiring/guard:** `tests/mcp/core/test_tool_docs.py` tool→test map includes
  `systems.profile_examples`; the generated tool-reference doc lists it; no new migration.

## Out of scope

- Versioning or persistence of profiles (the discovery tool returns examples, not stored
  templates).
- A general async-job pattern for MCP tools (provision already enqueues + returns; finding 3 is
  about not dropping the socket, not about turning fast tools into jobs).
- The ephemeral-probe-guest egress check (ADR-0091's separate guest-vantage probe); D adds only
  the server-vantage host-reachability probe.
- Re-enveloping every FastMCP boundary validation error for every tool (A converts it for the
  profile path; a project-wide boundary-validation middleware is a possible follow-up but not
  required by these findings).
