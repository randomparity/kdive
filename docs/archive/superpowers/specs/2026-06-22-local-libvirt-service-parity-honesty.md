# Local-libvirt service parity — the honest-surface foundation (M2.8 Epic A)

- **Date:** 2026-06-22
- **Milestone:** M2.8 — Local-libvirt service parity
  ([`../../design/m2.8-local-libvirt-service-parity.md`](../../design/m2.8-local-libvirt-service-parity.md))
- **ADRs:** [ADR-0208](../../adr/0208-provider-capability-descriptor.md) (capability descriptor),
  [ADR-0209](../../adr/0209-capability-aware-mcp-admission.md) (fail-fast admission +
  profile-resolved defaults). Static maturity reuses
  [ADR-0175](../../adr/0175-partial-tool-maturity-reason.md).
- **Status:** Approved (design)

## Problem

The MCP tool surface is provider-agnostic (ADR-0063): tools are registered once, the bound
provider is resolved at execution time. The default provider, `local_libvirt`, wires its
live-debug, introspection, and host-dump capture seams as `live_vm`-test-injected stubs —
production `from_env()` installs placeholders that raise `MISSING_DEPENDENCY` "only under the
live_vm gate." So an agent walking the catalog against a local System passes build→boot, then
fails on:

- `debug.start_session` — `LocalLibvirtConnect.from_env()` wires `_real_resolve_endpoint` /
  `_real_resolve_ssh_endpoint`, both of which raise `MISSING_DEPENDENCY` unconditionally; this
  strands every session-bound `debug.*` op.
- `introspect.from_vmcore` / `introspect.run` — drgn seams left `None`, raise up front.
- `vmcore.fetch` — **defaults its method to `HOST_DUMP`**, which local advertises in
  `supported_capture_methods` but whose `_real_host_dump_capture` seam is a stub. The obvious
  call (no method) passes admission, enqueues, then fails the job asynchronously.

The failures are deferred, opaque, and reference a test-harness concept. Epic A makes the surface
*tell the truth* and *fail fast* — independent of, and prerequisite to, wiring the planes (Epic B).
The fix must be provider-neutral so cloud/bare-metal/PowerVM inherit it for free.

## Decision (summary; full rationale in ADR-0208 / ADR-0209)

Three layers, landing as three issues.

### A1 — Provider capability descriptor (ADR-0208)

Generalize the ad-hoc `supported_capture_methods: frozenset[CaptureMethod]` field on
`ProviderRuntime` into a uniform descriptor by adding sibling fields:

- `supported_debug_transports: frozenset[DebugTransportKind]` — `{"gdbstub", "drgn-live"}` subset.
  `DebugTransportKind` is the **existing** `Literal["gdbstub", "drgn-live"]` in
  `providers/ports/lifecycle.py` (the `"drgn-live"` kind is ADR-0085) — reused, not introduced.
- `supported_introspection: frozenset[IntrospectionMode]` — `{"offline-vmcore", "live"}` subset.
  `IntrospectionMode` is a **new** `Literal["offline-vmcore", "live"]` in `providers/ports/`,
  mirroring `DebugTransportKind`.

No `default_capture_method` field is added: the per-System default `vmcore.fetch` resolves to is
owned by the existing `ProfilePolicy.capture_method(profile)` seam (see A2), not duplicated on the
descriptor.

All new fields default to **empty** (conservative: a partial/unconfigured provider reports *no*
capability, never a false positive). **A1 also flips the existing `supported_capture_methods`
default** from `frozenset(CaptureMethod)` (all methods, fail-open) to the empty frozenset so every
descriptor field shares one fail-closed rule; safe because all three providers already set it
explicitly (only permissive-default callers — test fixtures / ad-hoc `ProviderRuntime(...)` — must
now set it, caught at the type/test layer). Populate the descriptor in all three providers'
`composition.py`:

- **local** — `supported_capture_methods` is narrowed to the methods local can actually capture a
  core for: `{KDUMP}` now (+ `HOST_DUMP` once B4 lands). The currently-advertised `CONSOLE` and
  `GDBSTUB` members are **dropped** — they name non-core crash-handling modes that produce no
  vmcore (and are already excluded from `vmcore.fetch` by `_VMCORE_METHODS = {KDUMP, HOST_DUMP}`),
  so listing them in a capability the surface reads as "core methods you can fetch" is exactly the
  half-truth this milestone removes. `supported_debug_transports` and `supported_introspection`
  start **empty**, filled by B1/B2/B3.
- **remote** — populated from what it already implements (KDUMP/HOST_DUMP capture, gdbstub +
  drgn-live transports, offline + live introspection).
- **fault-inject** — populated from its synthetic capability.

Project the descriptor through `resources.describe` (and the `availability` projection) so an
agent can query per-System capability before acting. Read-only honesty; no behavior change beyond
reporting.

### A2 — Capability-aware admission + profile-resolved defaults (ADR-0209)

In `debug.start_session`, `introspect.from_vmcore`, `introspect.run`, and `vmcore.fetch`: resolve
the bound `ProviderRuntime` and check the requested plane/method against its descriptor **before**
enqueue/execution. On a miss, raise `CONFIGURATION_ERROR` with ADR-0174 detail:
`{reason: "capability_unsupported", capability, provider, supported: [...]}`.

Remove `vmcore.fetch`'s static `method = CaptureMethod.HOST_DUMP` default; resolve an omitted
method through the existing `ProfilePolicy.capture_method(profile)` seam, **clamped to the
core-producing methods** `{KDUMP, HOST_DUMP}` (the existing `_VMCORE_METHODS`). If the profile's
method is non-core (console/gdbstub System), there is no implicit core default, so the tool
requires an explicit core-producing `method`. An explicit method is validated against the
descriptor. The admission code reads `runtime.<field>` and never branches on `ResourceKind`.

### A3 — Static maturity metadata (reuse ADR-0175)

Mark `debug.*`, `introspect.*`, and the host-dump path `maturity: "partial"` via `maturity_meta()`
in `_docmeta.py`, each with a `MaturityReason`, one-line `detail`/`promotion`, and a `providers`
pointer ("local-libvirt: planned (M2.8 B*); remote-libvirt: implemented"). Update the
`test_tool_docs` drift guard. Pure metadata; each promotes to `"implemented"` in the Epic B PR
that wires its plane.

## Scope

- **In:** the descriptor fields + defaults on `ProviderRuntime`; population in all three
  `composition.py` providers; `resources.describe`/`availability` projection; capability-aware
  admission in the four tools; the `vmcore.fetch` default change; the maturity annotations +
  `test_tool_docs`.
- **Out:** wiring any real local seam (Epic B); any remote/fault-inject behavior change beyond
  populating the descriptor; new tools; schema/migration (none).

## Acceptance

### CI (host-free, fakes)

- A descriptor is present on every constructed `ProviderRuntime`; a unit test asserts each
  provider's composition reports the expected capability sets, and that an unconfigured runtime
  reports an empty frozenset for every capability field (including the now-empty-by-default
  `supported_capture_methods`).
- `resources.describe` projects the descriptor; a test asserts a local System reports
  build/boot/kdump and **not** debug/introspect/host-dump, and a remote System reports its full
  set.
- `debug.start_session` / `introspect.from_vmcore` / `introspect.run` / `vmcore.fetch` against a
  System whose bound descriptor lacks the plane raise `CONFIGURATION_ERROR` with
  `reason: capability_unsupported` and the supported set, **without** creating a job row /
  touching a seam (assert no enqueue).
- `vmcore.fetch` with no method resolves via `capture_method(profile)` clamped to the
  core-producing set (a crashkernel local System → KDUMP; a `preserve_on_crash` System → HOST_DUMP;
  a console-only System → requires an explicit core method); an explicit unsupported method is
  rejected up front.
- `test_tool_docs` passes with the new `partial` maturity + provider pointers; the generated
  `docs/guide/reference/*` regenerates.

### Live (development KVM host)

- Epic A is host-free; its live confirmation is implicit in Epic B — once a plane's descriptor
  flips and its seam is wired, the *same* admission code begins admitting it. No separate live
  drive for Epic A beyond confirming `resources.describe` reports the true (still-partial) local
  capability on a real System.

## References

- Milestone: [`../../design/m2.8-local-libvirt-service-parity.md`](../../design/m2.8-local-libvirt-service-parity.md)
- ADR-0208 (descriptor), ADR-0209 (admission), ADR-0175 (maturity), ADR-0063 (runtime seam),
  ADR-0174 (config-error detail), ADR-0097 (error categories).
- Epic B planes: ADR-0210 (live-debug + introspection), ADR-0211 (host-dump), ADR-0207 (#666 kdump).
