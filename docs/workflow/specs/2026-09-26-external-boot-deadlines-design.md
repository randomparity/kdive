# Local external-boot deadline containment (#2800)

## Problem

The local external-boot console poll defaults to 900 seconds, but activation, recovery, and
server-persisted release deadlines are 300 seconds. TCG scales neither the poll nor those
deadlines. Independent server, worker, and authority-host configuration files can disagree,
and provider XML can disagree with the System's recorded accelerator. A longer deadline
computed in one process alone would not prove that the provider's poll fits.

## Scope and contracts

Implement [ADR-0684](../../adr/0684-scale-external-boot-deadlines.md). New local jobs carry
`LocalExternalBootTimingV1` with schema `local-external-boot-timing-v1`,
`accel: "kvm" | "tcg" | None`, positive integer `console_window_s`, and integer
`deadline_budget_s > console_window_s`. Both integers are bounded by `timedelta.max` in
whole seconds. The producer reads
`System.accel` and the server configuration once, computes both as rounded-up scaled seconds,
and rejects non-finite, unrepresentable, or absolute-deadline-overflow values with an actionable
`configuration_error`. The default snapshots are KVM 900/1200 and TCG 9000/12000 seconds.

For activation, `build_external_boot_payload` stores the snapshot on optional
`BootPayload.local_timing`. For release and conflict resolution, `RecoveryRequestV1` stores it
beside the DB-clock absolute deadline; keyed replay returns the original job without
recomputation. Keep early keyed replay ahead of activation-state admission. New metadata is
created only after the selected activation and provider binding are known. Orphan repair keeps
its five-minute deadline because it does not open a console readiness window.

The worker reads the persisted snapshot and uses its budget when it commits a new activation
deadline after preparation. That `deadline` intermediate keeps the job running; the next
handler invocation creates a fresh authority client for the console poll, anchored to the
committed deadline plus 30 seconds for a transport return. It does not reuse the client
clock spent on preparation or renew the committed deadline on retry. Recovery client calls
use no more than the remaining absolute recovery deadline. Committed recovery attempt
deadlines are also never renewed by a retry.
`AuthorityMutationRequestV1` carries the optional snapshot. `JournalRecordV1` stores it, and
replay requires equality, so a retry cannot change a previously admitted window.

The local authority checks explicit snapshot accelerator against the owned inactive domain
XML before mutation. A `None` accelerator uses the larger TCG-safe scale for either XML
type. It computes its effective console window from its own settings with the snapshot
accelerator and requires equality with the snapshot window before mutation. A mismatch is
a local `configuration_error` before mutation; the existing authority transport reports
`provider_conflict` and logs that cause for the operator. `LocalLibvirtExternalBoot.activate/recover`, its
IO/session factory, and console creator then receive that validated snapshot window.
Historical requests without a snapshot use the previous behavior. Remote provider requests
retain the 300-second timeout and have no local timing snapshot.

The `runs.release_external_boot`, `systems.resolve_external_boot_conflict`, and
`ops.resolve_recovery_orphan` tool wrappers state the absolute server-clock deadline, its
per-request scope, expiry consequence, and recovery action. Orphan repair's five-minute text
remains accurate. Declare `server` as a consumer of `KDIVE_LIBVIRT_BOOT_WINDOW_S` and
`KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`, verify startup validation, and regenerate the config
reference to explain which process creates a new operation's snapshot. TCG target-defined
recovery remains a hard stop: no
remaining-time bound reaches the provider.

### Failure model

- Invalid or oversized timing rejects a new request before enqueue with `configuration_error`.
- Host-window or explicit accelerator disagreement rejects before provider mutation. The
  authority transport reports `provider_conflict` and logs the local configuration cause.
- A new request can expire after queue or transport delay; existing terminal recovery rules apply.
- Historical jobs keep their recorded deadlines and old provider timing.

## Success and validation

KVM, TCG, unknown accelerator, config changes, replay, retry, stale deadlines, provider XML
mismatch, and separate authority-host configuration have focused tests. They assert the
snapshot's `budget > window`, the host refuses a mismatched window, uses a matching snapshot
window, and the worker never extends a
persisted recovery deadline, and old/remote/orphan paths retain their prior timing. Run
`just lint`, `just type`, changed tests, generated config checks, and the full pre-push gate.
Live KVM/TCG proof is reported only when fixture-backed tiers run.
