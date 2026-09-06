# Authority-owned System teardown

## Goal

Route a System with any durable external-boot activation history through the
authority-marked teardown job, so a worker cannot use its ordinary libvirt
provider to destroy authority-owned host state.

## Scope

This change adds the durable route, teardown activation state, and the existing
authority commit's terminal-state record. A follow-up provider change performs
the local host mutation and supplies the exact ready-credit or pending-no-credit
proof inside the authenticated authority TEARDOWN commit. It does not add a
worker provider mutation or make recovery observation destructive.

## Contract

- The server resolves the newest activation for a System, including a completed
  release, to one durable `(provider_kind, authority_instance)` route. Missing
  or ambiguous authority configuration fails closed.
- A marked teardown uses the existing authority acknowledgement and
  positive-quiescence fence. Its current SQL completion accepts only the
  matching worker incarnation, attempt, authority generation, and result
  evidence; the provider follow-up binds its terminal journal-head proof.
- `external_boot_activations.state = torn_down` records physical teardown. It
  is distinct from `abandoned`, whose terminal evidence asserts abandonment.
- A ready reservation is credited only after exact cleanup evidence. A pending
  reservation is removed without a credit. Quarantine retains the reservation
  until its exact authority-owned disposition completes.
- A previously clean recovered or abandoned activation retains its existing
  cleanup and release evidence when its System is later torn down.

## Failure handling

The public route never falls back to ordinary teardown after finding external
boot history. A mismatched marker, stale worker, lease expiry, missing terminal
journal evidence, or malformed cleanup proof returns `superseded`/fails the
marked job without changing System or capacity state.

## Verification

Real-Postgres migration tests prove the new terminal state and preservation of
the existing authority commit fences. MCP tests prove the marked route is
selected for both active and clean historical activations and ordinary teardown
is unreachable for that history. The provider follow-up proves credits,
quarantine, and interrupted commit recovery.
