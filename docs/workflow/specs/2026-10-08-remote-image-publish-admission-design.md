# Remote image publication admission

## Problem and authority

Issue #3151 gives two outcomes: build/stage remote images or refuse before enqueue.
Epic #2803 requires real complete evidence and preserves native POWER qualification.
Human approval of the campaign row covers scope/exclusions. The operator approved
ADR-0759's concrete refusal interface and evidence obligations on 2026-10-12.

## Current behavior and cause

`mcp/tools/ops/images/build_publish.py::publish` enforces platform_operator then calls
`_enqueue_image_build` for every provider. `jobs/handlers/image_build.py` rejects every
non-local catalog before resolving the provider build plane. A bounded reproduction
runs these actual functions, replacing only the enqueue I/O boundary: remote admission
reaches enqueue once and worker resolution raises configuration_error. This proves the
control-flow cause, not a real queued-job or HTTP result.

RemoteRootfsBuildPlane builds a local Fedora disk and captures provenance; it has no
remote catalog/family selection or pool staging. Source search finds production
composition and provider tests, no direct CLI caller; rootfs CLI is local-only. Retain
the plane and composition, since deletion is unrelated to admission repair.

## Scope and chosen design

Adopt ADR-0759's upfront remote-only refusal. Preserve authorization ordering and
its denial audit. Return ToolResponse.failure(name, NOT_IMPLEMENTED) with fixed detail,
no refs and no next-job action. Do not invoke the enqueue helper on this path. No new
shared abstraction, runtime capability field, provider API or policy framework.
Existing worker refusal handles historical queued jobs unchanged. Unknown provider and
local catalog validation remain as before; this issue is not a generic validation rewrite.

Update owning wrapper/Field and generated reference/CLI artifacts plus both owning
runbooks. Do not change accepted ADRs in place. No authority publishing
#3131/#3124/#3125/#3135/#3152, native POWER #2818, or unrelated installation scope.

## Coverage obligations and actual proof

In coverage contract generation, apply one explicit reviewed remote images.publish rule
before constructing the existing functional cell: retain ID/scenario/owner and set kind
unsupported, reason ADR-0759, and observation to prove synchronous refusal/no mutation.
Keep both architectures' obligations; execute only x86_64. Rejection cells retain their
existing kind/assertions and clear unsupported_reason. No new capability rejection arm.
Only these named unsupported admission cells use roles=(server,), accelerator=none,
guest_arch=None, no guest OS/image/kernel inputs and actual server-host OS/architecture.
Preserve host_arch as the original obligation architecture and require actual host match;
x86 execution cannot fulfill the retained POWER IDs. Keep provider/config/exposure/IDs
unchanged. Bind them through a narrow admission-context branch before provider-lane
binding, excluding them from its remote guest/image path. Rejection cells retain all
existing metadata/bindings. No general context schema or qualification relaxation.

The existing run-tool carrier branches remote publication before its old blocked entry,
uses a fresh scoped image name and existing platform operator token/HttpCaller, snapshots
_publish_state before/after, and inspects the raw response (not success-only one()).
Require not_implemented, retryable false, no job refs/action, fixed reason and unchanged
catalog/build-job/platform state. Record effect and cleanup artifacts including actual
request/exposure and snapshots. CellRun.context supplies actual server-host identity and
accelerator none; do not call observe_host or lane_target for these admission cells.
Scenario routes unsupported functional IDs to this branch; run_tool_cell maps kind
unsupported to Outcome.UNSUPPORTED only when scenario returns successfully. Assertion or
transport failure stays failed/blocked, never unsupported merely from metadata.

Run all four actual x86_64 cells: default and recovery configurations, direct and gateway.
Also run affected remote role-denial/validation/authentication cells where the same
configured stack supports them. Preserve observed role/candidate identity and DB state.
The refusal precedes any provider resolution, so the four admission cells need no
provider host and run on the local demo-up stack in both configurations. Existing rejection cells still require their original lane target,
so execute those only under a separately scheduled applicable fixture grant.

## Failure model

- Missing stack/configuration/issuer/admission binding blocks proof, never substitutes
  unit results. Transport ambiguity fails the cell and retains before/after evidence.
- A buggy admission that enqueues is a failed proof. Retain exact scoped name/job IDs and
  request operator cleanup through existing mechanisms; do not fabricate clean state.
- Existing historical remote jobs may still fail at the worker. No purge or retroactive
  success is promised.
- Concurrent unrelated operators may mutate platform snapshots and fail the isolated
  proof; run in a scheduled exclusive stack window, do not weaken equality checks.
- Unsupported functionality remains unavailable; this is an explicit operator-approved
  boundary, not remote build/boot success.

## Success and validation

Authorization-first remote refusal is immediate with no enqueue or publication effects;
local success/idempotence and other-provider handling remain unchanged. All four remote
IDs are present, perform real HTTP calls and produce qualified unsupported records tied
to the exact candidate; missing calls, false success, mutations and absent reviewed rule
must fail causal tests. Wrapper discovery explains refusal and recovery in both exposures.
Focused database, wrapper, coverage-contract and carrier tests cover these contracts.
An actual-shaped CellRun.context → build_record → qualify regression must pass with
only server role/host observations, no guest or KVM; wrong host architecture, stale
candidate, missing reviewed rule and wrong outcome must fail. Preserve rejection-cell
metadata and their ordinary provider binding. A named binding unit test verifies the
admission branch never requests provider image/volume probes.
Run lint/type, docs/CLI generation guards and hooks before commit; installed mandatory
pre-push owns full just ci. Independent final/security review precedes delivery. No
release or native POWER qualification is inferred.
