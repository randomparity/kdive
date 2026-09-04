# Remote module appliance supervision design

Issue: #2169. Decision: [ADR-0589](../../adr/0589-remote-module-appliance-console-wait-is-idle-bounded.md).

## Goal and scope

Land the confined transient-domain supervisor already reviewed on the authorized #2129 lineage,
isolated from that branch, and correct its identity, deadline, and would-block defects. The runtime
renders and starts or adopts one network-free appliance, reads only redacted closed-vocabulary
console evidence, accepts completion only from the durable scratch result, and proves teardown.

The change consumes the document contracts already on `main` and the attachment-inspection and
prepared-volume contracts owned by #2167 and #2170. Those two dependencies must land before this
branch can be green and mergeable; this change does not copy or redefine their contracts. Sibling
operation orchestration, volume lifecycle, and native ppc64le live proof are out of scope.

## Contracts

- A foreign same-name domain fails with the validator's `ErrorCategory.CONFLICT`; the supervisor
  re-raises `CategorizedError` unchanged. Libvirt/XML failures without a category remain
  infrastructure failures at the caller boundary.
- One invocation has a monotonic 300-second outer deadline. Console reads have a 30-second idle
  deadline re-armed only after a non-empty byte chunk, never past the outer deadline.
- A `-2` would-block result yields for 10 milliseconds, capped by remaining idle time, and does not
  count as progress. EOF ends the read.
- Once `openConsole` resolves or fails synchronously, its allocated stream is aborted on every
  exit. An unresolved `openConsole` call is not followed by an abort that could race it; the
  appliance remains recoverable by retry. Console bytes are redacted before bounding and only
  validated, stable protocol `key=value` lines may leave the supervisor. Durable scratch results,
  not console text, decide success.
- Timeout or an unclassified fault attempts bounded teardown without claiming success. An
  unresolved binding call remains unresolved and is not followed by a competing mutation.

## Threat model

The authenticated tenant indirectly selects a System and operation, while the remote libvirt
administrator controls domain/storage state and appliance console bytes. Existing core authority
and #2167 attachment inspection constrain which root can be attached. Exact normalized domain
identity prevents adopting a foreign domain. Closed document validators constrain scratch input.
Console input is UTF-8-decoded safely, redacted, bounded to 16 KiB, and filtered to closed fields;
raw output and host paths never leave this boundary. Deadlines bound binding calls and loops.

Privileged libvirt-administrator interference is outside this provider's trust boundary, as in
ADR-0585. Native ppc64le execution and the appliance image's internal filesystem algorithm are
owned by their existing records and are not re-proven here.

## Verification

Focused tests reproduce the three issue findings before their fixes: foreign identity category,
progress beyond 30 seconds under the 300-second cap, and yielded would-block polling. A silent
would-block stream proves expiry at the 30-second idle boundary without re-arming. Synchronous
`openConsole` failure proves stream release; unresolved `openConsole` proves that no competing
abort starts. Existing tests retain domain-shape, redaction, stream-release, durable-result,
timeout, and teardown proofs. The provider test file, lint, whole-tree type checking, and the full
`just ci` gate must pass once #2167 and #2170 are present on the branch.
