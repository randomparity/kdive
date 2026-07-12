# ADR 0332 — `resources.availability` free-device list opt-in

- **Status:** Accepted
- **Date:** 2026-07-12
- **Deciders:** kdive maintainers
- **Follows:** [ADR-0070](0070-fleet-availability-system-reuse.md) — the fleet
  availability read whose per-host payload this change summarizes.
- **Prior art:** [ADR-0324](0324-runs-get-console-manifest-opt-in.md) — the same
  summarize-by-default / opt-in-full pattern applied to the `runs.get` console
  manifest.

## Context

`resources.availability` returns a per-host item that inlines the full free PCIe
inventory unconditionally (#1098, `BLACK_BOX_REVIEW.md` F7). `_host_item`
(`availability.py`) emits both `free_pcie` (the free device count) **and**
`free_devices` — the full redacted `{bdf, vendor_id, device_id, class_code}`
descriptor list — on every host, on every call. A single unfiltered call on a
dense fleet returned ~135 device rows inline, hundreds of lines of token cost.

The fields an agent needs for the common "where is there headroom" read are
`headroom`, `fits_now`, and `queue_depth`; the free device count `free_pcie` is a
cheap scalar. The full descriptor list only matters when the agent must pick a
specific device. The existing `pcie=` filter gates only whether a *host* is
included, not the detail level, so there was no way to get the cheap view.

## Decision

Summarize the per-host payload by default and gate the full device list behind an
explicit opt-in.

**Always report the count.** `free_pcie` (the free device count) stays on every
host item — it is a scalar and answers "does this host have a free device".

**Gate the list.** Add `include_devices: bool = False` to
`_ResourcesAvailabilityPayload` as a `Field(default=False)` with an agent-facing
description, and thread it through `availability_tool` and `_host_item`. When
`False` (default) the `free_devices` key is **omitted** from the item; when `True`
the full redacted descriptor list is returned, byte-identical to today's output
(still label-redacted per ADR-0070).

**Omit, don't empty.** When off, the key is absent rather than `[]` — an empty
list would falsely read as "this host has no free devices" when it simply was not
requested. `free_pcie` already carries the count.

**No schema/service change.** The change is confined to the tool's response
shaping and its request payload. The PCIe descriptor source, redaction, filter,
and fitting computation are untouched.

## Consequences

- The default `resources.availability` envelope shrinks by the entire free-device
  inventory, keeping the common fleet-headroom read cheap. `free_pcie`,
  `headroom`, `fits`, `fits_now`, and `queue_depth` are unchanged.
- A caller that must pick a specific device passes `include_devices=true` and gets
  today's exact per-host `free_devices` list.
- This is a **default-behavior flip on one envelope key**: any consumer that relied
  on `free_devices` always being present must now opt in. `free_pcie` (the count)
  remains unconditional.
- The generated tool reference (`docs/guide/reference/resources.md`) regenerates
  from the new `Field` description.

## Alternatives considered

- **Auto-include the list only when a `pcie=` filter is present** (the issue's
  alternative). Couples detail level to an unrelated filter and leaves the
  unfiltered common path — the one that hurts — no way to see a device it wants.
  A single explicit knob is predictable. Rejected.
- **Render `free_devices: []` when off.** Diverges from "absent means not
  requested" and misleads a caller into reading it as "no free devices."
  Rejected — omit the key, the count already conveys presence.
- **Paginate or cap the always-inline list.** Still spends tokens and shaping work
  on every call for data most callers ignore. Opt-in is the token win. Rejected.
- **Default the flag `true`** (opt-out). Preserves today's output but does not fix
  the reported cost — the point is that the common path be cheap. Rejected.
