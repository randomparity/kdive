# Run, System and Job service cells (#3097)

## Authority and scope

The operator approved section #3097 of the ten-design proposal, SHA256
`d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418`, on
2026-10-16. The frozen scope is issue #3097's current `WORK:SCOPE`, owned by
`q3097-cec5c011`, in campaign `2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18`.
The approved original scope/exclusion packet is SHA256
`5ce7042937eaa47a8e1cfb10ba07265e720dac9be3d037e655b927ac17b29b50`.

Implement the eleven named tools' 55 scenario bindings and 220 cells: 44 functional
and 176 rejection cells, including 24 filtering-list controls, over default/recovery
and direct/gateway. No cell is optional. Sources are #3097 Expected and #2803's real
state, identity, async-effect, cleanup and honest-outcome requirements.

Permitted files are the new integration carrier, its focused tests, the obligations
bindings, the three canonical empty pages in the shared harness, and these design
artifacts. Reuse the HTTP, evidence, rejection, investigation/upload cleanup and
local authority-System fixture seams. No ownership migration is needed. Existing
production contracts remain unchanged; no ADR is warranted. Reserved ADR0789 is unused.

Exclusions remain allocation/accounting/report carrier (#3098), CLI (#3099),
authority orphan/conflict construction (#3185), public-authority provision repairs
(#3144/#3148), and native POWER qualification (#2818). No migration, role/grant,
provider, schema or production behavior changes are authorized.

## Data and observations

Each functional call uses its cell exposure and minimum role. Fixture/setup calls
are separate and do not prove the tool. Each project is fresh. Read-only evidence
queries compare durable Run, Investigation, System, Allocation, build, artifact,
step and job records. HTTP upload URL PUTs store actual synthetic bundle/vmlinux
bytes; the build check independently hashes stored bytes and compares accepted
metadata. Synthetic success proves service validation, never kernel bootability.

Create covers bound and unbound relationships. Get compares the build result and
separate install/boot states. Set changes and clears a note while preserving state,
label and build. Bind compares ready System/allocation ownership and retains the
uploaded build. List observations compare ordered pages and each documented filter
with independent records, with visible and excluded controls. Systems.get compares
allocation, linked Runs and runtime capabilities with durable/configured facts.

jobs.wait creates a check_ssh_reachable job on a real full-stack System and polls
through the selected exposure to terminal success. Durable ownership and the worker
attempt plus an independent SSH/banner observation establish its effect.

jobs.cancel uses the existing local fixture-mode authority System and verified
prebuilt longterm build. An explicitly exclusive, initially unpaused and idle lane
may pause the queue through the existing ops tool. Enqueue valid runs.install,
require queued state, attempt zero, no worker holder, no install step/build-use or
authority operation, then cancel through the selected exposure. Compare System,
build, domain and storage facts before/after, restore the queue in finally, and
recheck no late claim or effect. A claimed job fails/blocks on #3241 without retry
or reclassification. This proves queued cancellation, not running-provider quiescence.

Rejections use the shared harness and unchanged protected state. Filtering-list
boundaries require a real visible positive item and exact canonical empty metadata.
Narrow protected snapshots include the selected terminal Job/Run, steps, build catalog
and build-use rows as well as live project state;
cleanup uses the existing frame inventory/capacity proof and investigation close,
Run cancellation and owned upload versions. Adopted reusable builds retain their exact
catalog-owned versions under the existing one-day grace: verify the durable close
cleanup_pending_at and expected retention, not post-grace collection. Unadopted
uploads are purged; ephemeral domain/storage/capacity restoration is a separate proof. Cleanup
failure preserves the primary error. Investigation close fences late create/finalize
publication before terminal-Run and version-specific adoption reconciliation. Queue
restoration first settles the exact owned idempotent install admission and canceled,
unclaimed job; an unknown outcome fails cleanup and explicitly holds the exclusive
lane for operator inspection rather than starting unresolved work. History rows are
retained, never deleted to make cleanup pass; live records and owned resources must be gone.

## Failure model

- Prevented: forged/missing authentication, insufficient role, foreign project and
  invalid arguments reach shared boundary checks; positive controls prevent vacuity.
- Detected: wrong bytes/digests, metadata/relations, note/lifecycle drift, filter or
  paging mistakes, claimed cancellation, changed domain/storage, cleanup leaks,
  missing deployed identities and missing prerequisites fail or block their cells.
- Accepted: synthetic service artifacts do not establish a bootable kernel;
  queued cancellation does not establish running-provider quiescence. These are
  the operator's selected observation boundaries, not omitted cells.
- Outside deployment: remote-authority provisioning and native POWER qualification
  belong to the explicitly excluded owners. The service contract retains both
  project target architectures; this live qualification requires a separately approved
  local x86 lane.

## Validation and operation gate

Focused tests inject digest, note/lifecycle, filter/page, positive-control,
noncanonical metadata, claimed-job and changed-domain faults into the actual
assertion predicates. Record meaningful red/green evidence. Run just lint, whole-tree
just type, relevant hooks and structural binding checks. Full-spec design review
and scope audit precede implementation; code review uses the iterating route.

A separate candidate-bound operation packet must identify the host, current deployed
server/worker/reconciler/authority revisions, fixtures, exclusive queue ownership,
resource/time bounds and finally cleanup. This code approval grants no guest,
queue pause, installation or build operation. The final local full gate is exactly
one managed installed pre-push just ci with regular captured UTF-8 streams and
stdin /dev/null. The native observer uses the existing authority UID, fixed protected
interpreter/argv,
strict UUID input and cooperative read-only libvirt API on the existing RW socket.
This adds no identity-only read grant and does not resolve #3237. Host preflight must
verify the exact sudo invocation and dependencies. No real live proof has run;
implementation alone cannot close #3097.
