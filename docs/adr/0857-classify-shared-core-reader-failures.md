# 0857 — Classify shared core-reader failures

## Status

Accepted (2026-11-24)

## Context

Shared dmesg extraction imports its helper and opens the core outside error conversion.
Local diagnostic fallback handles categorized errors; remote capture deliberately also
handles unexpected diagnostic faults. Missing helpers must remain operator failures.

## Decision

Classify missing drgn/helper imports at their lazy import boundaries. Categorize the
specified native opening and extraction errors in the existing shared reader, preserving
causes. Unexpected shared implementation faults propagate. Keep provider fallback policy,
fatal build-id validation and artifact handling with their existing owners. Update the
local fallback docstring; add no exception framework, dependency or new runtime owner.

## Consequences

Expected native failures reach the existing fallback consistently; missing dependencies
remain fatal remotely. The remote generic diagnostic fallback is retained, so unexpected
shared faults can still yield its established placeholder. Invalid-input native probes
supplement boundary doubles; they do not prove capture of a real kernel vmcore.

## Considered & rejected

- **Leave the shared boundary unchanged.** verified: at5078392171a7, the actual local fallback
  with its default shared reader and drgn absent raised uncategorized ModuleNotFoundError.
  With pinned drgn0.3.0 installed on Linux x86_64/Python3.14.8, Program.set_core_dump on a
  missing path raised FileNotFoundError and empty/invalid files raised ValueError;
  get_dmesg on an unbound Program raised LookupError. These calls preceded shared conversion.
- **Catch arbitrary exceptions in the shared reader.** judgment: obscures implementation
  failures rather than describing expected native data/I/O failures.
- **Remove remote's generic diagnostic fallback.** verified: existing
  test_host_dump_raw_dmesg_failure_degrades_to_placeholder requires that fallback while
  retaining build-id and captured core; removing it changes an independent existing policy.
- **Introduce a common exception/reader service.** judgment: two existing functions already
  own the native calls; another owner adds coordination without fixing another boundary.
