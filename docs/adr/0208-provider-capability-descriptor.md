# ADR 0208 — A provider-neutral capability descriptor on `ProviderRuntime`

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** M2.8 A1
- **Refines (does not supersede):** [ADR-0063](0063-typed-provider-runtime.md) (the typed
  `ProviderRuntime` port seam; this adds declarative capability data to that runtime, not a new
  port).
- **Builds on:** the existing `supported_capture_methods: frozenset[CaptureMethod]` field on
  `ProviderRuntime` — this ADR generalizes that one-off field into a small, uniform descriptor.
- **Spec:** [`../superpowers/specs/2026-06-22-local-libvirt-service-parity-honesty.md`](../superpowers/specs/2026-06-22-local-libvirt-service-parity-honesty.md)

## Context

The MCP tool surface is provider-agnostic (ADR-0063): a tool is registered once and the bound
provider is resolved downstream at job-execution time. This is the right design, but it means
the surface advertises a *uniform* capability the providers do **not** uniformly deliver. The
default provider, `local_libvirt`, wires its live-debug, introspection, and host-dump capture
seams as `live_vm`-test-injected stubs — production `from_env()` installs placeholders that
raise `MISSING_DEPENDENCY`, and the real implementations exist only as fixtures the test harness
injects. An agent walking the catalog against a local-libvirt System therefore passes
build→boot, then hits deferred `MISSING_DEPENDENCY` job failures the moment it tries
`debug.start_session`, `introspect.*`, or `vmcore.fetch` (whose default method, `HOST_DUMP`,
local cannot honor).

There is one precedent for capability data on the runtime —
`supported_capture_methods: frozenset[CaptureMethod]`, which `vmcore.fetch` already consults —
but it is a single ad-hoc field covering one plane, and it is not surfaced to agents through
`resources.describe`. There is also a *static* per-tool maturity model (ADR-0175): a tool can
be marked `maturity: partial` with a closed `MaturityReason` and a provider pointer. That
catalog metadata tells an agent "this tool is partial on some providers" but cannot answer the
operative question — "can **this** System, bound to **this** provider, do **this** right now?" —
because it is static per-tool, not per-provider-instance.

The capability question is not local-libvirt-specific. Every provider — `remote_libvirt`,
`fault_inject`, and the future cloud/bare-metal/PowerVM families — supports a different subset
of planes, and the surface must report each honestly without branching on provider kind. A
mechanism that only describes local's gaps would ossify around local's shape and force a re-plumb
when the next provider arrives.

## Decision

Generalize the ad-hoc `supported_capture_methods` field into a **uniform capability descriptor
carried by the universal `ProviderRuntime`**, declared once by each provider at composition and
read everywhere by the surface. The descriptor is the single source of truth for "what planes
does this provider support."

### 1. The descriptor is sibling frozensets/flags on `ProviderRuntime`

`ProviderRuntime` gains, alongside the existing `supported_capture_methods`:

- `supported_debug_transports: frozenset[DebugTransportKind]` — the live-debug transports the
  provider can open (`GDBSTUB`, `DRGN_LIVE`). Empty ⇒ `debug.start_session` is unsupported.
- `supported_introspection: frozenset[IntrospectionMode]` — `OFFLINE_VMCORE` (`introspect.from_vmcore`)
  and/or `LIVE` (`introspect.run`). Empty ⇒ the corresponding introspection tool is unsupported.

`supported_capture_methods` stays the host-dump/kdump/etc. authority and is read as part of the
same descriptor. The descriptor is **declarative data**, not a new port protocol — no provider
implements a new method; it populates fields. The capability vocabulary is the existing,
**extensible enums** (`CaptureMethod`, `DebugTransportKind`, and the new `IntrospectionMode`),
so a future provider with a genuinely new capability adds an enum variant and populates the set,
rather than introducing a parallel mechanism.

### 2. Conservative defaults — a partial/unconfigured provider reports *less*, never more

Every descriptor field defaults to **empty/false** on `ProviderRuntime`. A provider that has not
yet wired a plane (local-libvirt before its Epic B plane lands), or is constructed unconfigured,
reports *no* capability for it. The surface can therefore never advertise a stubbed plane as
working: under-reporting fails closed (an agent is told "unsupported" for something that turns
out to work — annoying but safe), while over-reporting would resurrect the exact trap this ADR
removes. A plane is added to its descriptor set **in the same change that wires its real seam**.

### 3. Every current provider populates the descriptor in this ADR's change

`local`, `remote`, and `fault_inject` each set the new fields in their existing
`build_*_runtime()` / `composition.py` construction: remote and fault-inject report what they
already implement; local reports build/boot/kdump now and empty debug/introspect/host-dump until
Epic B fills them in. Wiring all three at once **proves the contract generalizes** before any
future provider exists and prevents the descriptor from quietly forming around local's shape.

### 4. The surface reads the descriptor generically — no provider-kind branching

`resources.describe` (and the `availability` projection) projects the bound provider's descriptor
so an agent can ask "what can this System do?" before acting. The capability-aware MCP admission
(ADR-0209) reads the same descriptor to fail fast. Neither consumer branches on `ResourceKind`;
both read `runtime.<field>` from the resolved runtime. This is what makes the honesty mechanism
inherited for free by every future provider.

### 5. Declared in code, not discovered into the DB

The descriptor is a property a provider declares at composition time, not a fact discovered into
`resources.capabilities`. It describes the *provider implementation's* plane support (the same
for every System of that kind in a deployment), not host-specific discovered data like `vcpus`.
So it needs no column, CHECK, or migration; it lives on the in-memory runtime and is read from
the resolved runtime on each call.

## Consequences

- `resources.describe` reports honest per-System capability the moment this lands — read-only
  honesty that is correct for all three current providers, and for every future provider that
  populates the descriptor.
- ADR-0209's fail-fast admission has a single, uniform thing to consult; it never needs to know
  the provider kind or probe a seam.
- The change to `ProviderRuntime`'s shape ripples to all three `composition.py` providers (the
  expected change-surface) and to `resources.describe`; the **port interfaces are unchanged**, so
  the ADR-0063 portability claim holds and is now exercised across three providers at once.
- Local-libvirt's descriptor is **honest-but-incomplete** until Epic B: it reports build/boot/
  kdump and *not* debug/introspect/host-dump. That is the intended intermediate state — the
  surface tells the truth about a gap rather than hiding it.
- A future provider inherits honest capability reporting and fail-fast by **populating the
  descriptor only** — no MCP-layer or `describe` change. This is the falsifiable generality claim
  the M2.8 design rests on.

## Considered & rejected

- **A unified `ProviderCapabilities` object aggregating every plane.** Cleaner as a single
  source long-term, but it requires folding the existing `supported_capture_methods` and
  `DebugCapabilities` fields into a new type — a wider refactor for no immediate behavioral gain.
  YAGNI: the sibling-frozensets shape matches the field that already exists, and the fields *are*
  the descriptor (read as a set by `describe`). The aggregate can be introduced later if the
  field count grows enough to warrant it, without changing the contract.
- **Derive capability by detecting the stub seam at runtime** (e.g. a sentinel on the placeholder
  function). Rejected: implicit and fragile, couples the MCP layer to provider internals, and
  inverts the dependency — the surface would reverse-engineer capability instead of the provider
  declaring it. A declared descriptor is explicit and testable.
- **Lean only on the ADR-0175 static maturity metadata.** Rejected: static per-tool maturity
  cannot answer the per-System question (a tool partial on local but implemented on remote needs
  a per-provider answer for the System actually bound). The two are complementary — static
  maturity advertises the catalog-level truth; this descriptor answers per-System and drives
  fail-fast.
- **Discover the descriptor into `resources.capabilities`.** Rejected: plane support is a property
  of the provider implementation, not the host; persisting it would add a migration and a
  staleness hazard (a code change to a provider's capability would need a re-discovery to take
  effect) for data that is deterministic from the resolved runtime.
