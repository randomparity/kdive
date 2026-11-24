# 0864 — Give providers explicit console collaborators

## Status

Accepted (2026-11-24)

## Context

Local-libvirt leaves its console bundle unset. Three handlers interpret that absence as a local
file, while remote-libvirt supplies explicit adapters. Boot evidence additionally identifies a
fatal source read by its diagnostic operation string. Those mechanics belong to the provider.

## Decision

Wire local reader/snapshot adapters through the existing console bundle. Rename the reader
protocol to ConsoleReader and document the actual consumer redaction obligation. Local control
buffers retain raw byte offsets; remote retains ADR-0429 seam redaction. Final mandatory redaction
and caps remain at the existing control consumers. Local snapshots retain their redacted helper.
Missing collaborators mean unavailable, never local storage. Require advertised strict console
operations to have a reader. No console bundle on boot means no evidence.
Move local capture/store/upsert into its adapter. Express fatal categorized source reads using
ConsoleSnapshotReadError, preserving category, details, terminal state and original cause.
Keep other snapshot failures best-effort and retain each caller's freshness policy.

## Consequences

Worker handlers stop depending on local storage layout. Explicit construction may expose incomplete
test runtimes, which must supply real or faithful collaborators. There is no new raw output path,
threading, transaction or public API. Local readers support cumulative index0; remote part cursors
remain provider-specific. Callers must not treat internally read bytes as safe to publish directly.

## Considered & rejected

- **Keep absence-based local selection.** verified: at a2f5b45f8, diagnostic_sysrq._console_reader,
  watch_for_crash._console_reader and boot_evidence.capture_run_console each select local file
  mechanics from missing collaborators; local composition supplies no console bundle.
- **Redact local control bytes before marking.** judgment: changing byte lengths before existing
  offset/seam logic would couple this ownership move to a new capture algorithm.
- **Unify freshness policy in one helper.** judgment: SysRq, retrying watch and boot intentionally
  differ. Provider mechanics can move without hiding those caller decisions.
