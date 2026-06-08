# ADR 0073 — Forced secret resolution + end-to-end redaction validation (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0027](0027-safety-modules-secret-backend-impl.md) /
  [ADR-0012](0012-secret-backend.md) (the by-reference `SecretBackend` and the
  register-before-return invariant),
  [ADR-0072](0072-fault-injection-provider-seeded-engine.md) (the mock provider that does the
  resolving), [ADR-0071](0071-per-kind-provider-runtime-registry.md) (the registry that
  selects it), the cross-cutting redaction contract in
  [`../specs/top-level-design.md`](../specs/top-level-design.md) §Cross-cutting concerns.
- **Spec:** [`../specs/m1.5-fault-injection-provider.md`](../specs/m1.5-fault-injection-provider.md)

## Context

The secret-by-reference contract has two halves (top-level design §Cross-cutting concerns):

1. **Register before return** — `SecretBackend.resolve()` registers the resolved value into
   the `PROCESS_SECRET_REGISTRY` *before* returning it, so a `Redactor` built from that
   registry next will mask it. `FileRefBackend.resolve()` enforces this structurally: there
   is no return path that yields the value without first calling `registry.register(value,
   scope=...)`.
2. **Mask before persist** — every guest/transcript/console output passes through that
   `Redactor` before it lands in the object store or a response snippet, so the registered
   value is replaced by **exact-value** masking, not merely by secret-*name* patterns.

Half 1 is unit-tested against `FileRefBackend`. **Half 2 has no live caller.** The only
shipped provider, local-libvirt, resolves **no** secrets — a local QEMU domain needs no BMC
password, SSH credential, or HMC token — so no production path has ever run
`resolve() → emit the value into a captured transcript → Redactor masks it on persist →
assert the persisted/returned artifact is masked`. The contract that matters most in the
distributed model (a remote provider's console capturing a resolved BMC password) is exactly
the one M0–M1.4 never exercised. The top-level design names this precisely: M1.5 "forces
secret resolution."

## Decision

We will give the fault-injection provider a **secret reference it must resolve**, make it
**emit the resolved value into a captured transcript**, and **assert the value comes back
redacted** — exercising the full register→mask→persist loop, not just half 1.

- The fault-inject resource's `capabilities` jsonb carries a **`secret_ref`** (a synthetic
  "BMC password" / SSH key, a file under the allowlisted `KDIVE_SECRETS_ROOT`). The mock's
  `connect` (and/or `provision`) **resolves it through the runtime's `SecretBackend`**, which
  registers the value into the scoped registry before returning it (ADR-0027).
- The mock then **emits the resolved value into a captured console/gdb transcript** — the
  realistic failure mode: a real provider's console echoes a credential it just used. The
  transcript flows through the **normal persistence path** (the `Redactor` built from the
  same registry), so the test asserts the persisted artifact **and** any response snippet
  contain the **redaction placeholder**, never the raw value — proving **exact-value**
  masking end to end.
- The resolution runs at the **worker boundary** under a **scoped** registration (the op's
  lifetime), and the test also asserts the value is **released** from the registry when the
  scope ends (`registry.release(scope)`), so a resolved secret does not linger as a global
  redaction needle past the op that needed it.
- **Quarantine-before-redaction** (top-level design): output captured *before* registration
  completes is marked sensitive until redacted. The mock's emit-after-resolve ordering means
  the value is always registered first; a test that emits a *pre-resolution* line asserts it
  is **not** masked (there was no secret to mask yet) — pinning that registration ordering is
  what makes the masking sound, not incidental.

## Consequences

- The register→mask→persist loop has a **live, asserted caller** for the first time, on a
  provider that *needs* a secret — so when M2's remote-libvirt provider resolves a real SSH
  credential, the contract it depends on is already proven, not first-run in production.
- **A redaction gap is a finding surfaced now.** If any persistence path bypasses the
  `Redactor` (a snippet built before masking, an artifact stored raw), the mock's
  emit-and-assert catches it on a synthetic secret — before a real credential leaks.
- **New obligation: the worker must thread a per-op registry scope** to the
  `SecretBackend`, and release it at op end. local-libvirt is unaffected (it resolves
  nothing), but the seam (resolver carries a scope) is now exercised, de-risking M2.
- **No new DDL** — `secret_ref` is a `capabilities` jsonb key (ADR-0072); the secret file
  lives under the existing allowlisted secrets root (ADR-0027), so the test fixture writes a
  file, it is not a schema or API surface.
- The synthetic secret is **never a real credential** — it is fixture data under the test
  secrets root, so emitting it into a transcript to prove masking carries no disclosure risk.

## Alternatives considered

- **Assert only that `resolve()` registers** (half 1), skip the emit-and-mask. Cheaper, but
  it re-tests what `FileRefBackend`'s unit tests already cover and leaves the half that has
  **no** live caller — mask-before-persist — still unproven. Rejected: the unexercised half
  is the entire reason the milestone says "forces secret resolution."
- **Force resolution but do not emit the value** (resolve, discard). Proves the resolver is
  reachable but never drives a value *through* the `Redactor`, so a persistence path that
  bypasses masking would still pass. Rejected: the failure mode M1.5 must catch is a
  resolved value reaching the store unmasked, which only an emit-and-readback assertion
  detects.
- **Exercise redaction against a hand-crafted registry in a unit test** (no provider). Tests
  the `Redactor` in isolation but not the **worker-boundary wiring** — scope creation,
  backend threading, persistence-path coverage — which is exactly where a real integration
  bug hides. Rejected: M1.5 exists to prove the seam under the real spine, not the redactor
  unit.
