# 0600 — Local external boot owns a fresh readiness window

## Status

Proposed (2026-09-05)

## Context

ADR-0576 makes KDIVE truncate a worker-owned append-mode console inode before every local domain
start. The external-boot session did not join that invariant: its `start()` called `domain.create()`
directly, and its readiness callback received only a System ID after the start. Binding the existing
single-poll `_real_readiness` would therefore read a prior boot's marker and could report success
even when the new target boot panicked.

External boot also depends on qemu-guest-agent observation. New local definitions contain the
standard virtio channel, but definitions created before that renderer change do not. Silently
redefining them during an external-boot operation would mutate durable provider state outside the
provisioning lifecycle.

## Decision

The local external-boot session owns one readiness window at a time.

1. Before every session-owned `domain.create()`, the session requires the domain inactive and calls
   the ADR-0576 console-preparation seam. Preparation truncates the validated worker-owned inode to
   zero. Successful preparation is the anchor; byte-offset slicing is not restored.
2. A dedicated external-boot readiness probe polls the whole fresh window until ready, crashed,
   terminal, probe failure, or the configured `KDIVE_LIBVIRT_BOOT_WINDOW_S` deadline. It uses a
   monotonic clock and never extends that single deadline after a probe failure.
3. Each `start()` replaces the prior anchor. `readiness()` is fail-closed before a successful start.
   Running-power restoration follows the same prepare-before-create path.
4. Console reads are capped. Oversize evidence, read failure, and evidence that cannot be tied to
   the prepared window return a failed readiness result; they never retry from historical bytes.
5. Session opening parses the owned inactive definition and requires exactly one standard
   `org.qemu.guest_agent.0` virtio channel before opening or creating the artifact root. A missing or
   malformed channel raises terminal `READINESS_FAILURE` with a reprovision instruction and no host
   path or guest output.
6. Production session-mechanism construction binds the preparation and external-boot readiness
   probes. It does not bind `ProviderRuntime.external_boot`; #2246 owns that advertisement change.

## Consequences

Every external-boot start has the same current-boot-only evidence invariant as other local start
paths. A prior ready marker cannot satisfy a later start, and a panic in the new window wins over
readiness according to the existing classifier. The base KVM window remains operator-configurable;
this issue does not add a second timeout setting or TCG policy.

Legacy Systems must be reprovisioned once before external boot can mutate them. This is a deliberate
compatibility refusal rather than an automatic domain redefine. Rollback is a code revert; no
persisted schema or data is rewritten.

The append-mode file can still undergo operator-side rotation. A window that cannot be proven to be
the prepared window fails closed rather than reading a replacement or historical file.

## Considered & rejected

- **Restore cross-boot byte offsets.** verified: ADR-0258 removed local offsets because a stale
  prior size drops the head of a fresh truncated boot; ADR-0576 retains whole-window reads while
  moving truncation to the worker. Restoring offsets would recreate the superseded failure mode.
- **Silently redefine legacy domains to add the channel.** judgment: this mutates durable provider
  state outside provisioning and makes an observation prerequisite an implicit migration.
- **Keep the existing one-shot readiness callback.** verified: `_real_readiness` sleeps once and
  returns `answered=False`; its caller in the install path supplies the polling loop, while the
  external-boot caller performs one comparison.
- **Add a new external-boot timeout setting.** judgment: the existing local boot-window setting
  already expresses the same unit, clock, host scope, timeout consequence, and operator recovery.
