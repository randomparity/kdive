# ADR 0313 — Operator-gated guest egress on local-libvirt (`restrict=on` becomes a policy default)

- **Status:** Accepted
- **Date:** 2026-07-05
- **Deciders:** kdive maintainers
- **Issue:** [#1031](https://github.com/randomparity/kdive/issues/1031) (parent epic #998; relates #985)
- **Amends:** [ADR-0218](0218-local-libvirt-session-ssh-transport.md) §1 — the unconditional
  `restrict=on` on the local-libvirt SSH-forward NIC becomes the *default* of an operator policy,
  not an unconditional block.
- **Builds on:** [ADR-0187](0187-remote-libvirt-per-op-resource-selection.md) (the per-Resource
  `rebind_for_resource` seam that carries the allocated Resource's `name` into provider config
  resolution), [ADR-0112](0112-systems-inventory-config.md) (the `systems.toml` inventory descriptor
  and its `[[local_libvirt]]` block), [ADR-0281](0281-always-render-ssh-forward.md) (the SSH forward
  is rendered on every local domain).
- **Spec:** [`docs/specs/2026-07-05-local-libvirt-egress-optin-1031.md`](../specs/2026-07-05-local-libvirt-egress-optin-1031.md).

## Context

On `local-libvirt` the guest has **no outbound network egress**. `_append_ssh_forward`
(`providers/local_libvirt/lifecycle/xml.py`) renders the guest NIC with QEMU user-mode (SLIRP)
networking and `restrict=on`:

```
user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:<ssh_port>-:22
```

`restrict=on` isolates the guest to the inbound `hostfwd` SSH port only — it blocks **all**
guest-initiated outbound traffic, including NAT'd internet and the SLIRP DNS resolver at
`10.0.2.3`. This was a deliberate defense-in-depth decision in ADR-0218 §1: the NIC exists for the
drgn-live SSH control channel, and egress is blocked so an agent-supplied (untrusted) kernel cannot
use the NIC to phone home or reach the host network.

The consequence contradicts two shipped contracts:

- **#998** (epic): *"The guest is yours as root — install tools at runtime with the guest package
  manager."*
- **#985** (ADR-0312): the agent-selectable larger guest disk exists as headroom for a
  runtime-installed tracer toolchain (`trace-cmd`, `bpftrace`, `perf`, `gcc`, kernel-headers,
  `drgn`). The disk sizing shipped, but on local-libvirt it is **necessary but not sufficient**: the
  space is there, the network to fill it is not. Live during the #985 gate, a booted `debug` System
  could not resolve a mirror host (`Could not resolve host: mirrors.fedoraproject.org`); setting
  `/etc/resolv.conf` did not help — with `restrict=on` there is no route off the guest at all.

**Scope: local-libvirt only.** remote-libvirt uses an operator-staged base image (toolchain
pre-installed per image-content obligations) and the real host network via the guest-agent seam, so
it is not affected the same way.

The load-bearing design question is **who owns the decision to open egress**. An agent/project must
not be able to grant its own egress — that is a confused-deputy hazard: the entity that bears the
residual risk (the operator, whose network zone is the real enforcement boundary) must be the only
one who can grant the capability. So the knob belongs at the **operator/inventory layer**, not the
per-request `LibvirtProfile` an agent controls.

## Decision

Add an **operator-owned, per-Resource** opt-in `guest_egress` to the `[[local_libvirt]]`
`systems.toml` block. When enabled for the allocated Resource, the rendered NIC drops `restrict=on`
(emits `restrict=off`) so the guest gets normal SLIRP NAT + DNS (`10.0.2.3`) and can reach its
distro mirrors. When absent or `false` (the default), the NIC renders `restrict=on` exactly as
today — no behavior change.

### 1. The knob is a per-Resource inventory field, resolved op-time (no migration)

`LocalLibvirtInstance` (`inventory/model.py`) gains `guest_egress: bool = False`. It is resolved
**at provision time from `systems.toml`, keyed by the allocated Resource's `name`**, mirroring
remote-libvirt's `remote_config_for_resource` (ADR-0187). It is **not** persisted to the `resources`
table:

- local-libvirt resource rows are **discovery-created** and only *overlaid* by reconcile
  (`inventory/reconcile/resources.py::_overlay_one_local` merges just the concurrency cap into the
  `capabilities` jsonb); the table has no generic config column for an arbitrary new field, and the
  `[[local_libvirt]]` block itself is **optional** (discovery creates the resource with or without
  it).
- Persisting would spread the field across a migration + reconcile + a DB read at dispatch. The
  established, migration-free seam is op-time resolution from `systems.toml` — exactly what remote
  already does.

A new `local_guest_egress_for_resource(name)` loads the `[[local_libvirt]]` instances via the shared
inventory loader and returns the named instance's `guest_egress`, defaulting to **`False`
(egress off — the secure default)** when no block names that Resource. A *missing* file or a
*missing* block is legitimate absence → egress off, never an error; a *malformed* file already fails
the shared loader with `CONFIGURATION_ERROR` and that propagates (fail-fast on operator corruption,
identical to the reconciler's all-or-nothing contract).

### 2. Activate the existing per-Resource rebind seam for local

The resolver already calls `ProviderRuntime.for_resource(name)` at the per-op chokepoint, which
delegates to `rebind_for_resource(name)` (ADR-0187). local-libvirt sets **no** rebind hook today, so
the allocated Resource `name` reaches the seam and is discarded. This ADR sets that hook: local
`build_runtime` gains a `resource_name` parameter, resolves `guest_egress` for it, and threads it
into `LocalLibvirtProvisioning`; `rebind_for_resource = lambda name: build_runtime(..., resource_name=name)`.
This *activates* an existing extension point rather than inventing a new path.

### 3. Render `restrict` from the flag

`render_domain_xml` gains a keyword-only `guest_egress: bool = False`, passed to
`_append_ssh_forward`, which emits `restrict=off` when `guest_egress` is set and `restrict=on`
otherwise. Everything else about the NIC (loopback-only `hostfwd`, the pinned PCI slot, the
`virtio-net-pci` device) is unchanged. The forward still binds SSH to `127.0.0.1` only; egress
opens the guest's *outbound* path, not any new inbound host exposure.

### 4. The knob is unreachable from the request

`guest_egress` lives only in operator-owned `systems.toml`. It is not a `LibvirtProfile` /
`provider.local_libvirt.*` field, is not accepted by any allocation/provision tool, and is resolved
at the worker boundary — so an agent cannot grant its own egress. This is the confused-deputy
avoidance the issue requires.

## Consequences

- With the operator opt-in enabled for a local Resource, a booted local-libvirt System renders
  `restrict=off` and can `dnf`/`apt install` from its distro mirrors at runtime (the #998/#985
  contract holds on local).
- With the opt-in absent or `false`, the domain renders `restrict=on` — byte-for-byte the pre-change
  NIC. A render unit test pins both states; the default path is regression-guarded.
- **Residual threat the operator accepts.** With egress on, an agent-supplied (untrusted) kernel can
  use the NIC for outbound traffic. The QEMU-level block is no longer the boundary; the operator's
  **network-zone firewall** is. local-libvirt Systems typically run in lab micro-zones that already
  enforce egress at the network firewall, making the QEMU block redundant there — which is why the
  opt-in is safe to offer *to the operator*. The runbook documents this explicitly.
- No schema/migration, no new tool/param, no RBAC role, no `ErrorCategory`, and no config-env change.
  The only new persisted surface is one optional `systems.toml` field. remote-libvirt and
  fault-inject are untouched.
- Flipping `guest_egress` takes effect on the next **fresh** provision of a Resource (the rendered
  domain XML encodes `restrict`); it does not retrofit a still-running domain, the same
  provision-time-decision property ADR-0281 §Non-goals documents for the forward itself.

## Considered & rejected

- **A per-request `LibvirtProfile.guest_egress` field.** Rejected — it lets an agent grant its own
  egress (confused deputy); the operator bears the residual risk, so the operator must own the knob.
- **Pre-bake the standard toolchain into `kdive-ready` images (build-time egress).** Complementary
  and worth doing regardless — it reduces how often runtime egress is needed — but it does not cover
  ad-hoc / arbitrary installs, so it is not a full substitute for the runtime path this ADR enables.
  Not done here.
- **A curated host-side caching package proxy** (keep `restrict=on`, add a `guestfwd` to approved
  mirrors only). Tighter, but more to operate; deferred unless the open opt-in proves too broad
  (the issue's own recommendation).
- **Persist `guest_egress` into the `resources` `capabilities` jsonb via reconcile, read it back in
  the provision handler.** Rejected as heavier: it needs a reconcile-overlay change plus a DB read at
  dispatch and *still* needs the same xml.py threading, for a knob remote already resolves op-time
  from `systems.toml` with no migration. Mirror the established seam.
- **A global `KDIVE_LIBVIRT_*` env setting instead of a per-Resource field.** Rejected — it is
  operator-scoped and would work for the single-host case, but it cannot express per-Resource policy
  once local hosts are pooled (ADR-0186/#561), whereas the `[[local_libvirt]]` field is per-instance
  by construction and matches how every other per-Resource provider knob is declared.
- **Advertise egress availability in the capability descriptor / `profile_examples`.** Rejected as
  out of scope: the acceptance criteria ask only that runtime installs *work* when enabled and that
  docs state when they work; a descriptor field is speculative surface. Revisit if agents need to
  discover egress programmatically.
