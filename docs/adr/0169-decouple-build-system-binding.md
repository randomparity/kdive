# ADR 0169 — Decouple build submission from a provisioned system

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0026](0026-investigation-run-lifecycle.md) (the Run
  as the join of a System and an Investigation — this ADR makes the System half of that join
  deferrable, not the Investigation half), [ADR-0070](0070-fleet-availability-system-reuse.md)
  (the create-time System admission preconditions reused verbatim at bind time),
  [ADR-0071](0071-per-kind-provider-runtime-registry.md) (the `ResourceKind → ProviderRuntime`
  resolver;
  the build now selects on a Run-recorded kind instead of the System join),
  [ADR-0029](0029-build-plane-local-make.md) /
  [ADR-0099](0099-remote-build-host-targets.md) /
  [ADR-0101](0101-local-libvirt-remote-build-host.md) (build-host selection is already
  System-independent; the build's last System tie is removed here),
  [ADR-0019](0019-tool-response-envelope.md) (the response envelope / error taxonomy / the
  self-correcting-error idiom).
- **Spec:** [`../superpowers/specs/2026-06-18-decouple-build-system-binding.md`](../superpowers/specs/2026-06-18-decouple-build-system-binding.md)
- **Issue:** [#554](https://github.com/randomparity/kdive/issues/554)

## Context

`runs.create` (ADR-0026) admits only a `ready` System with an `active` Allocation, and the
`runs.system_id` column is `NOT NULL`, so a Run cannot exist without a provisioned System.
A build, however, runs on an independently-selected build host (`worker-local` / `ssh` /
`ephemeral_libvirt`, ADR-0099/0101), not on the System. At build time the only use of the
System is `ProviderResolver.runtime_for_run`, which walks `run → system → allocation →
resource.kind` to pick which builder to run.

Requiring a provisioned System before a build inverts cost: provisioning is the slow,
capacity-consuming step, and gating a build on it makes a black-box user allocate and hold a
target just to attempt a build that may fail instantly (#552), wasting the held allocation.
The build plane and the provisioning plane are separable; the coupling is largely incidental.

The coupling is not *entirely* incidental, though: builds are provider-specific.
`local-libvirt` produces `bzImage` + `vmlinux`; `remote-libvirt` produces a `.tar.gz` bundle
that remote-install gunzips. The builder, the artifact shape, and the installer must agree, so
a Run must commit to a resource **kind** at creation even when it has no System — but a kind is
a declaration, not a provisioned resource.

## Decision

1. **A Run records a `target_kind` and may be unbound.** `runs.system_id` becomes nullable; a
   new `runs.target_kind` column is `NOT NULL` for every Run. Migration 0042 backfills
   `target_kind` for existing (necessarily bound) Runs from their resource kind before setting
   `NOT NULL`. The valid kind set is the deployment's registered provider kinds (runtime
   state), so no SQL `CHECK` enumerates it — validation is in the service layer.

2. **`runs.create` accepts an optional `system_id` and `target_kind`.**
   - *Bound path* (`system_id` present): unchanged ADR-0026/0070 admission; `target_kind` is
     derived from the System's resource kind; an explicit, mismatched `target_kind` is
     `configuration_error`.
   - *Unbound path* (`system_id` absent): `target_kind` is required and validated against the
     registered kinds; the investigation and the build-host↔source compat check are validated;
     a `reuse_requirement` is rejected (no System to assert against); the Run is inserted with
     `system_id = NULL`. Only the INVESTIGATION lock is held — **no target capacity is
     debited.** That is the decoupling.

3. **The build selects its builder from `run.target_kind`,** via `resolver.resolve(kind)`,
   not from the System join. This removes the build's last System dependency.

4. **A new `runs.bind(run_id, system_id)` attaches a ready System to an unbound Run.** It
   reuses the create-time System admission (System ready, Allocation live, single project,
   one-Run-per-System, optional reuse assertion) — factored into a shared helper, not
   duplicated — and adds a **kind-match contract**: `system_resource_kind == run.target_kind`,
   else `configuration_error`. The bind writes `system_id` with an `IS NULL` compare-and-set so
   a concurrent double-bind is safe (the loser gets `transport_conflict`). Binding is one-shot:
   an already-bound Run is always rejected.

5. **`runs.install` / `runs.boot` reject an unbound Run** with `configuration_error`
   (`reason: run_not_bound`) and `suggested_next_actions=["runs.bind"]`, at both the MCP
   admission boundary (fail-fast) and the worker handler (defensive null-System guard).

6. **Discovery affordances keep `target_kind` agent-usable.** The missing/unknown-`target_kind`
   `runs.create` errors carry `available_target_kinds` (the resolver's existing `registered`
   detail — the self-correcting-error idiom), and `systems.list` / `inventory.list` expose each
   System's resource `kind`, so an agent can find a ready System of the committed kind to bind.

The decoupled lifecycle is `create (unbound) → build → bind → install → boot`; the bound
lifecycle `create (system_id) → build → install → boot` is unchanged.

## Consequences

- A build can be attempted without allocating or provisioning a target, so a fast-failing build
  (#552) no longer wastes held capacity. Provisioning cost is paid at `runs.bind`, immediately
  before install, when the kernel is known-built.
- The Run's identity now spans `created`-while-unbound; `system_id` is meaningful only from
  `bind` onward. The one-Run-per-System invariant is enforced at `bind` (and still at the bound
  `create`) rather than only at create.
- An unbound Run is not auto-reaped. The reconciler fails a Run when its System's Allocation is
  torn down; an unbound Run has neither, so it terminates only through its own lane (build
  failure or explicit `runs.cancel`) and keeps its Investigation `active` until then — the same
  way a bound non-terminal Run does. Artifact retention is unchanged: a `succeeded` unbound Run
  holds its `kernel_ref` exactly as a `succeeded` bound Run does. No separate unbound-Run quota
  is added; the build-host capacity lease already bounds the work a Run can do.
- `target_kind` is a new required field on every Run and a new contract: the eventual System
  must match it. A Run that commits to a kind no System of that kind ever becomes available is
  buildable but not bootable — the agent observes this via `systems.list` returning no ready
  System of that kind, not via a create-time failure.
- `create_run` and the build handler now take the `ProviderResolver` to validate / select on
  kind; this is additive wiring at the two call sites.
- The bound path is behaviourally unchanged, so existing callers and tests need no migration of
  intent (only the additive `target_kind` field on the result).

## Considered & rejected

- **Overload `runs.install` with an optional `system_id`** instead of a dedicated `runs.bind`.
  Rejected: install would carry both the synchronous System-admission/lock dance and the
  worker-job trigger — two responsibilities — and the careful ALLOCATION→SYSTEM→INVESTIGATION
  lock ordering would migrate into the install boundary. A dedicated `runs.bind` mirrors
  `runs.create`'s admission and keeps install/boot almost untouched.
- **Relax only the readiness check** (keep `system_id` required at create but accept a
  not-yet-`ready` System). Rejected: it still holds an Allocation and provisions a target, so it
  does not decouple from provisioning cost — it fails the issue's intent.
- **Infer `target_kind` from the build host** rather than an explicit parameter. Rejected:
  build-host topology and target provider kind are independent today (a `worker-local` host can
  build for `remote-libvirt`); inferring would re-couple them and silently mis-target.
- **Carry `target_kind` inside the `build_profile` jsonb.** Rejected: it mixes "what to build"
  with "where it targets," is not directly queryable, and the kind is a Run-level binding
  contract, not a build input.
- **Default `target_kind` to the sole registered kind on single-provider deployments.**
  Rejected: a deployment that later registers a second provider would silently mis-target
  pre-existing unbound Runs; an explicit, discoverable required value is safer.
- **A standalone `resources.kinds` discovery tool.** Rejected as premature: the self-correcting
  `runs.create` error places the valid set exactly where an agent needs it, needs no new tool
  registration, and cannot drift from what is actually registered.
- **A SQL `CHECK` constraint enumerating kinds.** Rejected: the valid set is the *registered*
  provider kinds (runtime/deployment state), not a fixed schema vocabulary; encoding it in SQL
  would require a migration whenever a provider is added.
</content>
