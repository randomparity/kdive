# Direct catalog capability callers

## Problem

Three one-return forwarding helpers obscure existing capability and matcher owners.
The catalog cap helper describes degradation implemented by its sole caller.

## Scope

Remove catalog availability._resolve_cap and admission.pcie_claim.descriptors_for
and resolve_union. Existing owners remain ResourceCapabilities.allocation_cap /
pcie_descriptors and domain.pcie.resolve_multiset; no new ownership decision.
Availability._host_item reads its capability directly and explains missing/invalid
cap degradation beside schedulable. Preserve cap=0, headroom and fits behavior.
Availability._free_descriptors, admission.core._resolve_pcie_claim and
placement.resolve_placement_candidates read descriptors from their resource views.
The latter two call resolve_multiset directly with unchanged specs/claims.
Retain active_claims and its consumers; remove only unused imports and forwarding
prose. Fully qualify the catalog's active_claims docstring reference.
Remove four descriptor and five matcher wrapper-only tests; retain occupancy
fixtures/tests. Carry ordered valid-descriptor survival, non-string fields,
missing-key/non-list cases into existing ResourceCapabilities tests. Parameterize
catalog missing/malformed cap coverage. No public import contract or shim remains.
Global constraints: Python3.14/uv; targets x86_64/ppc64le; local commits only.

### Failure model

Current catalog, placement and admission callers retain existing trust validation,
configuration-versus-capacity outcomes and Resource lock around grant-time read,
match and persistence. Placement remains preliminary; admission rechecks. Host data
may be missing/malformed; catalog degrades one host while admission remains strict.
No SQL, lock, parser, permission or accepted wire behavior changes are authorized.

## Success

All six actual forwarding call references use their existing owners; the three
wrappers and obsolete tests disappear. active_claims and grant lock bodies remain.
Filtering order, repeated-device uniqueness, releasing occupancy, busy candidate
fallback and missing-device rejection remain proven. No new abstraction or dependency.

## Validation

Mode task-test-not-applicable: pure forwarding deletion has no changed executable
behavior to require red. Source inspection/AST proves each old body is one return
of its replacement; no implementation-mirroring regression is added for removal.
Mode focused-test: strengthen existing descriptor and missing/malformed cap cases,
run them before/after and inject dropped-descriptor / bad-cap-schedulability faults
that must fail, then restore. These are preservation tests, expected green before.
Run KDIVE_REQUIRE_DOCKER=1 just test-verbose against
 tests/mcp/catalog/test_availability_tools.py
 tests/domain/catalog/test_resource_capabilities.py tests/domain/test_pcie.py
 tests/services/test_pcie_claim.py tests/services/test_admission_pcie.py
 tests/services/allocation/test_placement.py.
Include concurrent last-device admission, releasing claims and placement busy/missing
cases. Compare active_claims and lock-context ASTs; run lint, whole-tree types,
exact staging/hooks and review the diff before a conventional local commit.
