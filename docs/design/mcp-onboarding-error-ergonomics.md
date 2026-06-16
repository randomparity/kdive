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
- **`detail` is a new client egress — message hygiene is part of the contract.** Exception
  messages in this codebase interpolate runtime values (e.g.
  `ProjectMembershipDenied(f"project {project!r} is not granted to {ctx.principal!r}")`,
  `src/kdive/security/authz/context.py:72`). The rule: a message surfaced as `detail` must be
  author-controlled and must not interpolate secrets, secret-ref paths, internal hostnames, or
  object-store keys. `CategorizedError` raise sites that fail this rule are fixed as part of work
  item B (the audit is bounded — only categories that reach `detail`, see the seam rule below).
  No automatic redaction pass is added; the discipline is "don't put it in the message."
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
- **No-leak guard (load-bearing) — enforced at the seam, not at the raise site.** The seam
  (`failure`/`failure_from_error`) holds a closed set of *suppressed categories*
  (`authorization_denied`, `not_found`): for those, `detail` is set to a fixed constant
  (`"access denied"` / `"not found"`) and `str(exc)` is **ignored**, so no raise site can leak
  through `detail` even if its message embeds the named resource. `detail` is populated from
  `str(exc)` only for the diagnostic categories (`configuration_error`, `missing_dependency`,
  `build_failure`, …). This is a single seam check, not a distributed invariant across every
  raise site. A regression test asserts a non-member denial and a by-id `not_found` both carry
  the constant `detail` with no project/resource name and (for `not_found`) preserve the ADR-0097
  existence-no-leak property.

### Work item A — Provisioning-profile discoverability (ADR-0124, finding 1)

Two cooperating changes; the discovery tool is the guaranteed-working half.

- **Typed parameter:** `systems.define`/`systems.provision` type `profile` as
  `ProvisioningProfile` so FastMCP advertises its JSON schema (required fields, the discriminated
  `rootfs`, the provider sections). `ProvisioningProfile` is `extra="forbid"`
  (`src/kdive/profiles/provisioning.py:69`), so FastMCP validates *and rejects* malformed or
  extra-key profiles **at argument binding** — before the tool body and before the
  `_runtime_resolution` catch that builds our envelope. Two consequences must be designed, not
  assumed: (a) interception, below; (b) extra keys a caller could previously send (the old
  `additionalProperties: true`) are now rejected — an intended tightening, called out so it is not
  a surprise regression.
- **Two gating spikes (the typed-param half ships only if both pass):**
  1. **Interception** — confirm a binding-time `pydantic.ValidationError` can be converted into
     our `configuration_error` envelope (with the call's `object_id`/`allocation_id`, which binds
     fine as a separate `str` param). The candidate seam is FastMCP middleware
     (`TelemetryMiddleware`/`DenialAuditMiddleware`) or a FastMCP error hook; this is **unverified**
     and load-bearing — if binding failures cannot be re-enveloped, the typed param *regresses*
     malformed-input handling on the most important tool to a raw FastMCP error.
  2. **Client rendering** — confirm the FastMCP 3.4.0 client renders a `$defs`/`discriminator`
     **input** schema usably (ADR-0113 flattened recursive *output* schemas for this client;
     `ProvisioningProfile` is not self-recursive, but inputs are unverified for this client).
- **Fallback if either spike fails (preserves today's good error path):** keep `profile` as
  `Mapping[str, object]` (validation stays in our `parse()` → envelope path, unchanged) and
  advertise the schema by attaching `ProvisioningProfile.model_json_schema()` to the param via
  FastMCP `json_schema_extra` — schema discoverability *without* moving validation to the
  boundary. The discovery tool below ships regardless, so finding 1 is closed either way.
- **Discovery tool `systems.profile_examples`** (modeled on `projects.list`, ADR-0117):
  read-only, auth-only, no project gate, no audit. Returns one item per configured provider with
  a ready-to-edit example profile dict. Where the `systems.toml` inventory supplies a usable
  reference, the example uses the real name (e.g. a remote-libvirt `base_image_volume`, a
  local-libvirt `catalog` image declared in inventory); where it does not, the example carries a
  clearly-marked placeholder reference (and a `note`) the caller must replace. **The examples are
  schema-and-policy valid, not necessarily provisionable as-is:** a placeholder rootfs path won't
  exist on a host, `kind:"upload"` is only accepted by `systems.define` (it opens an upload
  window) and rejected by `systems.provision`, and a `catalog` ref must name a real inventory
  image — so the tool's contract is "this parses and passes provider policy; fill in the marked
  references for your host," not "paste this and it boots." Chains into `systems.define` /
  `allocations.request` via `suggested_next_actions`.

### Work item D — Diagnostics host reachability (ADR-0125, finding 4)

- Wire the existing `ProviderTlsCheck`/`GdbstubAclCheck` into the default diagnostics service
  factory (closing the deferred "egress-probe wave" gap).
- Add a remote-libvirt **reachability check**: open `remote_connection()` and call
  `conn.getInfo()` under a bounded per-check timeout, reusing the `SshBuildHostProber` pattern
  (`asyncio.to_thread` + timeout). Report `pass`/`fail`/`error` with the connection failure
  category (`transport_failure` vs `configuration_error`).
- **Probe scope (anti-amplification):** `remote_connection()` materializes TLS certs and opens a
  libvirt connection — not free, and authz-gated does not mean rate-limited. The probe targets a
  **single** `[[remote_libvirt]]` instance, selected by an optional `host`/`instance` argument; it
  does **not** fan out to every configured instance on one call (which would turn one cheap MCP
  call into N TLS handshakes against remote hosts). With no argument it probes the inventory's
  default/sole instance. The per-check timeout bounds a hung host (a probe cannot stall the report
  past it). Gated like the other diagnostics checks (ADR-0091).
- **Scope of the claim:** a successful connection proves the host is **libvirt-reachable**, not
  that it is provision-ready — a reachable-but-misconfigured host (missing storage pool/network)
  still reports `pass` and fails later at provision. The check distinguishes "unreachable/bad
  transport" from "reachable"; config-usability failures remain a provision-time signal (now
  legible via work item B's `detail`).

### Work item C — Synchronous-tool transport bound (ADR-0126, finding 3)

- **Audit + offload:** find the synchronous blocking call(s) in the `systems.provision` request
  path (DB lock acquisition / libvirt call during admission) and offload to `asyncio.to_thread`
  so the event loop is never blocked while one request runs.
- **The timeout must not orphan a mutation (load-bearing).** Python cannot kill a running thread:
  an `asyncio.wait_for` over a `to_thread` future abandons the *future* but the thread runs to
  completion. `systems.provision` mutates state (mints the System row, enqueues the provision
  job), and `transport_failure` is `retryable=True` (`src/kdive/mcp/responses.py:45`). A naive
  "wrap the body in a timeout → return `transport_failure`" therefore lets the mutation land in
  the background while the caller is told it failed and **auto-retries → a duplicate
  System/allocation/job**. So the bound is applied **only to the pre-mutation segment** of the
  request path (validation, admission checks, lock acquisition) — the segment that legitimately
  blocks the event loop and where a timeout is safe because no state has changed yet. Once the
  first mutation begins, the request runs to its own completion and returns its real envelope; it
  is never abandoned by the dispatch timeout. (The fast mutation+enqueue itself is sub-second; the
  observed stall was in the pre-mutation segment.)
- **Idempotency backstop:** even with the segmented bound, a client that retries after a genuine
  transport drop must not double-provision. `systems.provision`/`define` carry an idempotency key
  (the existing idempotency ledger, ADR-0016) so a retried identical request is deduped rather
  than minting a second System. If the ledger does not already cover this path, adding it is part
  of work item C.
- **Spike (gates the threshold and the segment boundary):** reproduce the stall to confirm *which*
  call blocks and that it is in the pre-mutation segment, and set the timeout above the legitimate
  worst case for that segment.

## Failure modes and edges

| Condition | Result |
|---|---|
| Bad profile via typed param (interception spike passed) | Boundary `ValidationError` → `configuration_error` envelope + `detail` + `errors` field-paths |
| Either typed-param spike fails | Fallback: param stays `Mapping`, schema advertised via `json_schema_extra`; our `parse()`→envelope path is unchanged; `systems.profile_examples` still serves an example |
| Caller sends extra keys (old `additionalProperties:true`) | Now rejected (`extra="forbid"`) — intended tightening |
| `authorization_denied` / non-member denial | Seam forces constant `detail` (`"access denied"`); `str(exc)` ignored; no project/resource name leaks |
| `not_found` by id | Seam forces constant `detail` (`"not found"`); ADR-0097 existence-no-leak preserved |
| Structured error with >20 sub-errors | First 20 surfaced; bounded so the envelope stays small |
| `systems.provision` stalls in the pre-mutation segment | `transport_failure` envelope, not a dropped socket |
| Timeout would fire after a mutation began | Cannot — bound covers only the pre-mutation segment; the request completes and returns its real envelope |
| Client retries a genuine transport drop | Idempotency ledger (ADR-0016) dedups; no duplicate System/allocation/job |
| Remote-libvirt host down | reachability check reports `fail` + `transport_failure` (single targeted instance) |
| Remote-libvirt misconfigured URI/cert | reachability check reports `error` + `configuration_error` |
| Remote-libvirt reachable but not provision-ready (no storage pool) | reachability reports `pass`; failure surfaces at provision with a legible `detail` |
| `ops.diagnostics` called with no host arg, multiple instances configured | Probes the default/sole instance only; no fan-out |
| `systems.profile_examples` with no usable inventory ref | Example carries a marked placeholder + `note`; still schema-and-policy valid, not provisionable as-is |

## Test plan (behavior, not implementation)

- **B:** a rejected `configuration_error` carries a non-empty `detail` and an `errors` list with
  field paths; an `authorization_denied` non-member denial **and** a by-id `not_found` each carry
  the seam's constant `detail` with no project/resource name (the seam ignores `str(exc)` for
  those categories — asserted with a raise site whose message *does* embed the name, proving the
  seam, not the raiser, enforces it); the `errors` list is bounded at 20 and input values never
  echo.
- **A:** the advertised `systems.define` input schema is no longer `additionalProperties: true`
  (typed-param path: a `$ref` schema; fallback path: the `json_schema_extra` schema); a malformed
  profile returns the `configuration_error` envelope, not a raw FastMCP error; **each
  `systems.profile_examples` example, with its placeholders resolved to inventory references,
  passes `ProvisioningProfile.parse()` + `validate_profile_for_provider()`** (the schema+policy
  layer — not the full allocation-scoped admission path, which also enforces sizing/upload-window
  and so cannot isolate schema validity).
- **D:** the default diagnostics factory now includes the TLS/ACL checks; against a single
  targeted instance, a reachable host reports `pass`, an unreachable host reports `fail` +
  `transport_failure`, a bad URI reports `error` + `configuration_error`; a no-host call with
  multiple instances configured probes only the default instance (no fan-out).
- **C:** a stall in the pre-mutation segment returns a `transport_failure` envelope rather than
  dropping; the provision request path runs no sync blocking call on the event loop (asserted by
  an injected slow DB/libvirt double not stalling a concurrent request); a retried identical
  provision after a transport drop is deduped by the idempotency ledger (no second System).
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
