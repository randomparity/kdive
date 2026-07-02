# ADR 0297 — Pin a v2-capable guest CPU on remote-libvirt domains via host-model

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** KDIVE maintainers

## Context

Issue #975 is the remote-libvirt parity of #956 (ADR-0294). ADR-0294 root-caused an
EL9/RHEL-family reachability failure to a missing `<cpu>` element in the local-libvirt
domain XML: with no `<cpu>`, libvirt/QEMU fall back to the default `qemu64` model, which
is **x86-64-v1**. EL9/RHEL-family glibc requires **x86-64-v2**, so an EL9 guest's `ld.so`
aborts PID 1 (`Fatal glibc error: CPU does not support x86-64-v2`) before any userspace —
no NIC, no sshd — and the guest is unreachable. Debian's v1 baseline booted regardless,
which masked the defect until the per-family live test caught it.

`remote_libvirt` has the **same shape**. Neither renderer emits a `<cpu>` element:

- `providers/remote_libvirt/lifecycle/xml.py::render_domain_xml` — the per-System domain.
- `providers/remote_libvirt/lifecycle/build_vm.py::render_build_domain_xml` — the
  ephemeral build VM.

If a remote host's default QEMU CPU model is x86-64-v1 and it provisions an EL9/RHEL-family
guest (System or build VM), it hits the identical init panic and is unreachable. ADR-0294
flagged this parity gap explicitly and deferred it to a separate issue because reproducing
and proving it needs a remote libvirt host, which was not available in the #956 work
environment.

The one material difference from #956 is fleet topology. Local-libvirt is a single-host,
co-located, ephemeral debug provider; ADR-0294 pinned `<cpu mode='host-passthrough'/>`
there because there is exactly one host, no migration, and the debug intent wants the
host's full ISA. A remote-libvirt deployment is an operator-configured fleet that may span
**heterogeneous** physical hosts. That difference drives the CPU-mode choice below.

## Decision

Mirror ADR-0294's fix in the provider, choosing the CPU mode that fits a multi-host remote
fleet, and prove it with a gated live test on a remote host.

1. **Emit `<cpu mode='host-model'/>` on both remote domain renderers.** Add the element to
   `render_domain_xml` (System) and `render_build_domain_xml` (build VM), placed after
   `<vcpu>` and before `<os>`, matching the local renderer's ordering. `host-model` asks
   libvirt to synthesize a portable named baseline close to the running host's CPU; on any
   modern KVM host that baseline is **x86-64-v2 or better**, so every supported guest
   family — including EL9 — boots past its glibc baseline check.

2. **Prefer `host-model` over `host-passthrough` for remote.** Unlike the single-host
   local provider, a remote fleet may be heterogeneous. `host-model` produces a
   host-independent, migratable CPU definition that still satisfies v2, so a guest defined
   against one host is portable across the fleet. `host-passthrough` would tie each guest
   to the exact CPU of the host it was defined on — acceptable for the local single-host
   debug VM, but a portability foot-gun on a multi-host fleet. This is the divergence #975
   anticipated ("remote may prefer host-model/named-model over host-passthrough if guests
   migrate across heterogeneous remote hosts").

3. **Prove it with a gated remote live test.** Add a `live_vm`-marked test that renders and
   defines a remote domain on an operator-provided remote host and asserts an EL9 guest
   **boots past init**, proven by the always-present signal: the qemu-guest-agent channel
   answering (`wait_for_agent` / `wait_for_agent_responsive`), which drains only if the
   guest reached userspace. This is deliberately *not* an SSH-reachability assertion:
   unlike local-libvirt's always-rendered loopback forward (ADR-0281/0294), remote-libvirt
   renders an SSH forward only when the operator configures `ssh_addr`/`ssh_range`
   (ADR-0291) and is guest-agent-only by default, and the build VM has no SSH path at all.
   Agent responsiveness is the liveness gate both `provisioning.py` and `build_vm.py`
   already use, so an EL9 image that panics at init hangs it to timeout — that is the
   regression signal. The test skips cleanly when the remote host/image env is absent (the
   ADR-0035 §4 skip idiom). The unit tests assert both renderers carry
   `<cpu mode='host-model'>`.

The change is a pure domain-XML addition: no schema/migration, no tool, no RBAC, no
error-category, and no config change. It un-gates nothing.

## Consequences

- **Fixed.** EL9/RHEL-family remote guests (System and build VM) boot past the glibc
  x86-64-v2 barrier and are reachable, closing the #956 parity gap for remote-libvirt.

- **Guests are portable across the remote fleet.** `host-model` yields a host-independent
  CPU definition, so a domain is not pinned to the CPU of the host it was first defined on.
  This preserves the option of migration or redefinition across heterogeneous hosts, which
  a fleet provider may need.

- **The effective guest ISA now depends on the landing host.** Because `host-model` tracks
  each host's real CPU, the guest's actual feature set varies across a heterogeneous fleet.
  An agent selecting a System cannot currently see which CPU model/capabilities it will get.
  ADR-0294 did not have this exposure (single host). This is a real gap in the selection
  surface, tracked as a **follow-up issue** (advertise available CPU models/capabilities at
  System selection time); it is out of scope for the reachability fix.

- **Slightly less ISA than host-passthrough for debug.** `host-model` omits host CPU
  features that lack a portable model representation, so the guest sees marginally less than
  the host's full ISA. For remote debug/introspection this is an accepted trade for fleet
  portability; it remains ≥ v2, which is what reachability requires.

- **The fix requires the remote host CPU to be ≥ x86-64-v2.** `host-model` reflects the
  defining host's real CPU, so if an operator's remote host is itself pre-v2 (an older CPU
  lacking the v2 feature set), the synthesized model is v1-class and an EL9 guest panics at
  init exactly as before — the element is present but buys nothing, and the domain still
  starts (no define/start-time error). This is the same latent dependency as ADR-0294's
  host-passthrough, but more plausible on a remote fleet of arbitrary operator hardware. It
  surfaces as the fix's own regression signal: the guest-agent never answers and the
  liveness gate times out (guest unreachable), indistinguishable from any other init
  failure. Operators running EL9 guests must provide v2-capable remote hosts. (A named
  `x86-64-v2` model — rejected below — would instead fail fast at domain start on a v1 host;
  host-model trades that diagnosability for richer ISA on capable hosts.)

- **Residual risk.** As with ADR-0294 the proof is operator-run: CI cannot boot a guest.
  Per-family remote coverage is only as complete as the images and remote host the operator
  seeds; the test's skip messages call this out.

## Considered & rejected

- **`<cpu mode='host-passthrough'/>` (mirror ADR-0294 verbatim).** Rejected for remote:
  host-passthrough copies the exact host CPU into the guest, tying the domain to the host it
  was defined on. That is correct for the local single-host provider but a portability
  foot-gun on a heterogeneous remote fleet. host-model satisfies the same v2 requirement
  while staying host-independent.

- **A named `x86-64-v2` custom model.** Pins exactly the EL9 minimum and is maximally
  deterministic/portable. Rejected: it caps the guest at the v2 baseline even on hosts with
  richer ISAs (worse for the drgn/crash/gdb debug intent) and buys stricter determinism the
  fleet does not require. host-model already satisfies v2 on any modern host while tracking
  real host capability. Its one advantage — failing fast at domain start on a pre-v2 host
  rather than booting into a silent init panic (see Consequences) — does not outweigh
  capping the ISA on every capable host; a pre-v2 remote host is an operator misconfiguration
  the guest-agent-timeout signal already surfaces.

- **Fix only the System domain, not the build VM.** Rejected: `render_build_domain_xml` has
  the identical missing-`<cpu>` shape, and an EL9-based build image would panic PID 1 the
  same way. Both renderers get the element.

- **Declare EL9 unsupported on remote and only document the gap.** Rejected: Rocky/CentOS/
  RHEL are a supported family (#823, ADR-0251) and the fix is one XML element per renderer.

- **Advertise per-host CPU capabilities as part of this change.** Rejected as scope creep:
  the selection-surface gap is a genuine new requirement (see Consequences) but is
  independent of the reachability fix and needs its own design (which discovery/describe
  surface carries it). Split to a follow-up issue.
