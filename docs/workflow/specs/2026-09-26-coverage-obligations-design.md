# Coverage obligations and qualification evidence

Issue: [#2804](https://github.com/randomparity/kdive/issues/2804), foundation of
[#2803](https://github.com/randomparity/kdive/issues/2803).
Scope: [q2804-f42697b7](https://github.com/randomparity/kdive/issues/2804#issuecomment-5850505782).

## Problem

The census builds one registration configuration. The result grid reduces evidence to one
tool/provider verdict, with duplicate results silently overwriting one another. Neither defines
the obligations that a qualification must satisfy independently of collection and execution.

## Scope and ownership

Keep inventory in `scripts/coverage_campaign/gridgen.py`. Add a reviewed, versioned operation
mapping beside it and expand that mapping against the registered configurations, rootfs catalog,
architecture traits and provider support objects. Replace the internal result representation in
`results.py`; its callers are its tests. Keep the existing campaign driver separate: printing a
tool envelope is not a scenario producer. No production API, provider or database behavior changes.

The operator approved these exclusions on 2026-09-26: fixture preparation and kernel builds
(#2805–2806); live scenarios and host reprovisioning (#2807–2817); native POWER qualification
(#2818); scheduling and publication enforcement (#2819); product corrections owned by #2718,
#2739, #2740, #2763, #2767, #2784 and #2681; new architectures/providers, a testing service and
production schema changes (project owner, outside #2803). Exclusion does not remove a required
cell. An absent implementation remains unproven and blocks complete qualification.

## Design

### Inventory and reviewed mapping (C1)

Build the offline app with and without its worker-death verifier. Record configuration membership
for each tool, including the gateway and operator tools. Read the registered tool objects before
request-time exposure filtering; direct and gateway invocation remain distinct obligations.
Derive image rows with `load_rootfs_catalog`, architectures with `SUPPORTED_ARCHES`, and provider
capabilities from the local and remote runtime factories' `ProviderSupport` objects. These factories
construct ports without contacting provider hosts. Inventory is not proof that a host supports them.

A checked-in version-1 mapping explicitly names each tool and its owner, functional assertion,
boundary assertions, and scenario identity. New registered names without an explicit mapping fail
the fast check. Removed/unknown names, duplicate identities, empty assertions, and unknown mapping
versions fail too. Prefix matching must not silently give a newly registered tool coverage.

Mappings distinguish pending scenarios from implemented pytest node IDs. Pending scenarios have
an owner issue and concrete assertion contracts, but no invented executable location. Implemented
scenarios need an explicit node ID and must satisfy the same evidence requirements. The fast check
verifies ownership/completeness, not live outcomes or collection success.

Expand expected cells independently of result files and pytest collection:

- Provider-independent tool operations need one real-service lane, with required registration and
  exposure variants. Provider-dependent operations have native local/remote lanes on the supported
  architectures. Boundary assertions have separate identities from functional assertions.
- Each catalog image gets its declared native-architecture smoke obligation, including build images
  with a purpose-specific assertion. Native x86_64/KVM and ppc64le/KVM-HV remain separate.
- Deep lifecycle obligations cover Debian/Ubuntu, Fedora, Enterprise Linux and SUSE where catalog
  rows exist, both providers and supported native architectures. Exact fixture bindings remain
  pending until the owning entry supplies them; this does not select representative images here.
- Documented foreign-architecture TCG boot/upload obligations remain distinct from native results.
  Architecture-specific capture restrictions use the architecture/capability owners; unavailable
  native prerequisites are blocked, not replaced by TCG.
- The epic's clean-host, interrupted-operation/resource-limit, and kernel-corpus obligations retain
  pending owner-backed scenarios for their later entries. They cannot disappear from qualification
  merely because an implementation has not landed.

The matrix identity hashes the versioned mappings plus canonical owner-derived inventory and
expanded cells. A tool/configuration, catalog, capability or assertion change therefore invalidates
older results. Do not hash host addresses, credentials or environment-specific private inventories.

### Evidence and qualification (C2, C3)

Use installed Pydantic for strict versioned JSON input validation; add no dependency. Qualification
takes the generated contract, an independently supplied candidate/input binding, and scenario
results. The candidate is a full Git commit ID. Input bindings name immutable image, kernel,
config, compiler and build identities for each applicable lane; unresolved bindings cannot qualify.

Each result identifies its expected cell, scenario and pytest node; candidate and deployed-role
commits; matrix and input identities; host/guest OS and architecture; actual accelerator; image and
kernel digests, kernel source/config/compiler/build identity when applicable; duration; assertion
evidence and content-addressed artifact references. Required deployed roles belong to the scenario
contract, not the submitted result: server for HTTP, worker/reconciler for provider effects, and
authority where involved. Unknown, missing or mismatched required revisions fail closed.

Functional success requires the contract's observable assertions, including terminal effects and
owned-resource cleanup for live mutations. A successful envelope, invalid-ID probe or an unrelated
passing assertion does not satisfy them. A rejection needs its separate expected error/contract
assertion and evidence that protected state did not change. Rejection cannot replace functionality.

Keep six outcomes: functional success, verified expected rejection, reviewed unsupported/not
applicable, failure, blocked infrastructure, and not run. A skip maps to not run or blocked, never
success. A reviewed unsupported decision belongs to the contract and cites its capability or
contract authority; a result cannot create an exemption. A known-defect or missing-prerequisite
record cannot qualify through either an exemption or a rejection.

Reject duplicate and unexpected results rather than choosing a winner. Materialize missing results
as not run. Qualification is green only when the entire required set has admissible matching
evidence. Report all failures and omissions in stable cell order; reject malformed inputs with an
actionable field/category diagnostic that does not echo submitted values.

### Entry points and publication safety (C3, C4)

Provide one module CLI with a fast `check` command and a `qualify` command, plus a deterministic
result-grid renderer. `check` exits zero for complete ownership even while scenarios are pending;
its output explicitly says live qualification is unproven. `qualify` exits nonzero for an incomplete
or invalid required set. Wire the fast command into a `just` recipe, `just ci`, and its individual
CI step. Do not enable release publication enforcement in this PR.

Published reports use contract-owned cell labels, closed outcome/reason codes, commit/digest
identities and sanitized platform identities. They omit raw submitted strings, host addresses,
credentials, local paths and private inventories. Artifacts are referenced by digest, not lab URLs.
Retain detailed private producer diagnostics outside this public report. Result files do not
execute a node ID, import a submitted module, fetch a URL or deserialize arbitrary objects.

## Failure model

1. **Actors and deployments**
   - Maintainers run the offline CLI locally or in trusted CI; reviewed pytest producers submit
     files. No network-facing service or privileged runner is introduced.
2. **Invariants and assets at stake**
   - Required cells cannot disappear through collection, skips, duplicate overwrite or omission.
   - Stale/mismatched evidence, failed scenarios and unknown revisions cannot qualify a candidate.
   - Native evidence cannot be replaced by emulation; public reports cannot expose private inputs.
3. **Accepted failure classes**
   - A malicious maintainer can forge an assertion and matching digest in a local evidence file;
     this offline accounting contract is not remote attestation or artifact signing.
   - No live success exists initially for pending mappings; nonqualification is the intended result.
4. **Covered elsewhere**
   - Real terminal-effect/cleanup assertions and deployed-identity acquisition: scenario owners
     #2807–2818. Their absence remains not run, blocked or failing here.
   - Trusted scheduling, evidence artifact retention and release enforcement: #2819.
   - Product defects keep the owners listed under Scope and cannot become exemptions.

## Validation

Run focused pytest tests for both registration configurations, unmapped additions, catalog and
capability expansion, pending mappings, duplicate keys and matrix identity changes. Exercise the
qualification API/CLI using a small complete synthetic contract: a positive baseline, followed by
controlled omitted-result, skipped-scenario, stale-fixture, mismatched deployed SHA, wrong
scenario/node, wrong architecture/accelerator, failed assertion and failed-cleanup mutations.
Each mutation must make the previously green decision red. Test role omission, unknown versions,
extra/duplicate records, invalid field types, attempted exemptions/rejection substitution, and
public-report redaction. Synthetic fixtures prove checker behavior, never live platform behavior.

Run the shipped fast command against the real registry/catalog and run qualification without live
results to report the actual pending inventory. Run `just lint`, whole-tree `just type`, affected
documentation checks and the installed pre-push gate. Linux verification uses the operator-supplied
x86 development capacity; no live VM mutation or native POWER proof is part of this change.
