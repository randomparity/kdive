# ADR 0243 — Owner-fetchable raw vmcore + vmlinux egress (#781)

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** kdive maintainers
- **Issue:** [#781](https://github.com/randomparity/kdive/issues/781) (epic [#764](https://github.com/randomparity/kdive/issues/764))
- **Spec:** [`../superpowers/specs/2026-06-25-fetchable-vmcore-vmlinux-design.md`](../superpowers/specs/2026-06-25-fetchable-vmcore-vmlinux-design.md)
- **Builds on (does not supersede):** [ADR-0140](0140-artifacts-get-content-retrieval.md)
  (the `REDACTED`-only inline/download gate on `artifacts.get`, **unchanged** — it still
  governs inline content and search), [ADR-0240](0240-live-drgn-script-introspection.md)
  (the live analog: dump-content self-protection is not a control; this is the offline half it
  named as #781), [ADR-0006](0006-oidc-rbac-attribution.md) /
  [ADR-0062](0062-platform-operations.md) (project-scoped RBAC roles and the audited role-denial
  this gates on), [ADR-0075](0075-objectstore-quarantine-pre-registration-writes.md)
  (the artifact sensitivity model this leaves intact).

## Context

The raw `vmcore` is stored `SENSITIVE` (`owner_kind='systems'`); the Run's `vmlinux` debuginfo is
`SENSITIVE` too, carried as `runs.debuginfo_ref` (and, on the external-build path only, an
`owner_kind='runs'` `artifacts` row). The artifacts read surface — `artifacts.get` /
`artifacts.search_text` / `artifacts.list` — hard-gates egress on `sensitivity == REDACTED`
(ADR-0140). So the owning project's own agent **cannot download its raw core or vmlinux at all**;
only the redacted dmesg derivative egresses.

For developer-controlled debug VMs, redacting the dump from its own owner protects nothing.
ADR-0240 already settled the principle on the live path: an agent that can run drgn against its own
kernel already reads 100% of that kernel's memory, so self-protection is not a control. ADR-0240
named this issue, #781, as the **offline analog** — a captured core is a static file, so the
principled path is to make it fetchable and let the agent run `drgn`/`nm`/`gdb` locally with full
power, instead of forcing every offline investigation through a constrained server-side API.

The `REDACTED`-only gate was conflating three concerns: (1) cross-project confidentiality (project
B must not fetch project A's core) — a genuine boundary; (2) platform-secret redaction (the managed
SSH key, registered secrets) — also genuine, but those are by-reference values that are never
stored as vmcore/vmlinux artifacts; and (3) dump-content self-redaction — the security theater this
ADR drops. Only (3) changes.

## Decision

We will add an MCP tool **`artifacts.fetch_raw(run_id, asset)`** (`asset ∈ {"vmcore",
"vmlinux"}`) that mints a presigned download URL for the Run's owner-fetchable raw debug asset,
gated by **project membership plus the `contributor` role on the Run's project** — not by
sensitivity. The raw vmcore and vmlinux stay classified `SENSITIVE`.

1. **The closed `asset` enum is the egress allow-list.** Only `vmcore` and `vmlinux` are
   resolvable. `rootfs`, `kernel`, `initrd`, `effective_config`, and every other `SENSITIVE`
   artifact stay non-egressable; making a new kind fetchable is a deliberate enum addition reviewed
   as an egress change (fail-closed).
2. **Owner-addressed resolution, uniform across both build paths.** `vmlinux` resolves from
   `runs.debuginfo_ref`; `vmcore` from `raw_vmcore_key(run.system_id)` (the existing raw-core
   resolver, which already excludes the `-redacted` sibling). Both handles are set by the server
   **and** external build paths, so the tool behaves identically for either — unlike fetch-by
   `artifact_id`, which only the external path backs with a row.
3. **Membership + `contributor`; cross-project isolation preserved.** The handler resolves
   `run.project`; a non-member receives a not-found-shaped response (existence masked), and a member
   below `contributor` is denied and audited. The `vmcore` branch gates on the System's **own**
   project — `require_role(contributor, system.project)` — since the core is the System's asset, not
   an assumed Run/System project invariant. This is the cross-project boundary the `REDACTED`-only
   gate was conflating with self-protection.
4. **URL-only, no inline bytes.** These are multi-GB binaries; the tool returns a presigned download
   URL (TTL `KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS`) plus size metadata. The inline-content and
   `search_text` paths stay `REDACTED`-only and untouched.
5. **Egress is audited.** Each successful fetch records an audit event — a raw-`SENSITIVE`-asset
   download is security-relevant.
6. **Platform-secret redaction is untouched.** The `SecretRegistry` / `Redactor` machinery (inline
   console / gdb / OTel output) is not on this path, and secrets are never stored as
   vmcore/vmlinux artifacts, so this egress cannot expose them.

## Consequences

- The owning project's agent downloads its raw core + vmlinux and runs `drgn`/`nm`/`gdb` locally
  with full power — the offline analog of ADR-0240, with no constrained server-side API in the way.
- `SENSITIVE` stops doubling as self-protection for these two kinds. Its remaining meaning —
  "not inline-serveable, not search-serveable" — is still enforced by the unchanged `REDACTED` gate
  on `artifacts.get`/`search_text`.
- The asset allow-list is now load-bearing security state. Any new `SENSITIVE` artifact kind is
  non-egressable by default; widening it is an explicit, reviewed enum change.
- vmcore multiplicity stays **per-System** (one core per System; a second capture on the same System
  is refused, not overwritten — `vmcore.py`). Per-Run vmcore capture is deferred to
  [#796](https://github.com/randomparity/kdive/issues/796); when it lands, the `vmcore` branch's
  resolver swaps from `run.system_id → raw_vmcore_key` to addressing by `run_id` directly. This ADR
  is forward-compatible with that change.
- No migration, no new env var, and no change to the capture or build write paths.

## Alternatives considered

- **A new `owner_fetchable` column on `artifacts`** (producer marks intent at write time). Rejected:
  a migration plus a change at every artifact write site and new object metadata, for no gain over a
  closed `asset` enum that already names the two kinds. The enum is the same allow-list with less
  surface.
- **Reclassify raw vmcore + vmlinux to `REDACTED`.** Rejected: it corrupts the sensitivity
  vocabulary (`REDACTED` means secrets-scrubbed, which a raw core is not) and would route multi-GB
  raw binaries through the inline-content and `search_text` paths the `REDACTED` gate admits.
- **Fetch by `artifact_id` (extend `artifacts.get`).** Rejected: `vmlinux` has an `artifacts` row
  only on the external-build path; the server build path carries it solely as `runs.debuginfo_ref`,
  so id-addressing would silently fail server builds. It would also entangle a second role gate
  inside the existing `viewer`/`REDACTED` tool.
- **Bundle vmcore + vmlinux into one call keyed by `run_id`.** Rejected: an atomic one-asset-per-
  call primitive is simpler to gate, test, and reason about, and avoids partial-result envelopes;
  the agent issues two cheap calls.
- **Server-mediated offline drgn (mirroring the live path).** Rejected by #762 / ADR-0240's own
  framing: a captured core is a static fetchable file, so the principled path is local execution on
  the fetched file, not a constrained server API.
