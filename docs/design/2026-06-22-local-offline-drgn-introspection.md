# Spec — local-libvirt offline drgn introspection (`introspect.from_vmcore`)

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../README.md).
> The interim maturity decision below does not establish current tool maturity.

- **Issue:** #676 (M2.8 Epic B, B2)
- **ADR:** [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) §2 (anchor;
  do not re-decide). Builds on [ADR-0033](../adr/0033-drgn-introspection-from-vmcore.md)
  (the offline introspection contract), [ADR-0208](../adr/0208-provider-capability-descriptor.md)
  (the descriptor each plane flips on as it lands), and
  [ADR-0209](../adr/0209-capability-aware-mcp-admission.md) (the fail-fast that gates the plane
  until it is wired).
- **Design doc:** [m2.8-local-libvirt-service-parity](../design/m2.8-local-libvirt-service-parity.md)
- **Status:** Accepted

## Historical decision: maturity stays `partial` (deliberate deviation from ADR-0210's literal text)

ADR-0210 and design-doc invariant 2 say each B plane "promotes its maturity in the same PR that
wires it." Design-doc **invariant 5** says live capability is *proven on hardware* before maturity
promotes to `"implemented"`, and CI cannot drive a real KVM host. These two invariants are in
tension for the seam-wiring PR; the milestone resolves it by separating two distinct facts the
surface reports:

- **The descriptor** (`supported_introspection`) states *the seam is wired* — the code path now
  exists and admission should admit it. This flips here. Reporting it as still-absent after wiring
  would itself be a lie (the negative direction of invariant 2).
- **The tool maturity** (`partial` → `implemented`, ADR-0175) states *the wired path is proven*.
  Per invariant 5 that requires the live KVM run, which is B6 (#680), not this PR.

So this PR keeps `introspect.from_vmcore` at `maturity: "partial"`, updates its `providers` pointer
to state local-libvirt is **wired, pending live KVM proof (M2.8 B6 #680)** rather than the
pre-wiring "planned (M2.8 B2)", and leaves the `implemented` promotion to the orchestrator's
post-merge live run. The catalog therefore never claims more than is true: admission admits the
plane (it is wired), and the maturity flag still tells an agent the path is not yet hardware-proven.

The honesty drift-guard moves with the state, and the replacement must be **provably at least as
strong** as the guard it removes. The removed guard
(`test_local_stubbed_planes_advertise_planned_provider_pointer`) asserts both `local-libvirt:
planned` *and* `remote-libvirt: implemented` are present in the pointer. `introspect.from_vmcore`
leaves `_LOCAL_PLANNED_PROVIDER_TOOLS`, and a new positive assertion on its pointer asserts:

- **present:** the stable marker `local-libvirt: wired` (the post-wiring, pre-promotion state) and
  `remote-libvirt: implemented`;
- **absent:** both `local-libvirt: planned` and `local-libvirt: implemented`.

Requiring the absence of *both* `planned` and `implemented` means the new guard cannot be satisfied
by an unchanged pre-wiring pointer (`planned`) **or** by a future over-promotion to `implemented`
before the B6 live proof — so it is strictly stronger than the substring it replaces, not weaker.
The pointer's exact wording is therefore: `local-libvirt: wired, pending live KVM proof (M2.8 B6
#680); remote-libvirt: implemented; fault-inject: n/a.`
