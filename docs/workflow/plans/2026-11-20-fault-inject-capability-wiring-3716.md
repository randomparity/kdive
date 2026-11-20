# Plan: fault-inject capability wiring (#3716)

Spec: [design](../specs/2026-11-20-fault-inject-capability-wiring-3716-design.md).

1. Red acceptance tests (done): `tests/providers/fault_inject/test_capability_wiring.py`.
2. `providers/core/runtime.py`: optional hook field; `for_resource(name, capabilities=None)`.
3. `providers/core/resolver.py`: select `r.capabilities`; pass it through the four lookups;
   update test fakes in `tests/providers/test_resolver.py` and any other fake rows.
4. `fault_inject/lifecycle/hotplug.py`: `with_behaviour` sharing ledger state.
5. `fault_inject/composition.py`: build shared ports once; per-binding assembly; set hook.
   `assembly/composition.py`: no change expected beyond the descriptor staying as is.
6. ADR-0074 amendment; fix stale text in `fault_inject/capabilities.py` and `discovery.py`.
7. Guardrails: `just lint`, `just type`, `just test-changed`, then `just ci`.
