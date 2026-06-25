# Fetchable raw vmcore + vmlinux (egress) — design

- **Issue:** [#781](https://github.com/randomparity/kdive/issues/781) (epic [#764](https://github.com/randomparity/kdive/issues/764))
- **ADR:** [ADR-0243](../../adr/0243-owner-fetchable-vmcore-vmlinux.md)
- **Date:** 2026-06-25
- **Status:** Accepted

## Problem

The raw `vmcore` (`SENSITIVE`, `owner_kind='systems'`) and the Run's `vmlinux` debuginfo
(`SENSITIVE`, carried as `runs.debuginfo_ref`) cannot be downloaded by the owning project's own
agent. The artifacts read surface (`artifacts.get` / `artifacts.search_text` / `artifacts.list`)
hard-gates egress on `sensitivity == REDACTED` (ADR-0140), so only the redacted dmesg derivative
egresses. For a developer-controlled debug VM this blocks offline kernel investigation without
protecting anything: an agent that can run drgn against its own dump already reads 100% of its
memory (ADR-0240 settled this for the live path). The captured core is a static file, so the
principled path is to make it fetchable and let the agent run `drgn`/`nm`/`gdb` locally.

## Goals

- An agent with project membership and the `contributor` role can download its Run's raw vmcore and
  vmlinux as presigned URLs and run drgn locally.
- An agent from another project cannot fetch them (cross-project isolation preserved).
- Platform-secret redaction (managed SSH key, registered secrets) is unaffected.

## Non-goals

- Changing the inline-content / `search_text` egress, which stays `REDACTED`-only (ADR-0140).
- Per-Run vmcore capture (multiple cores per System) — deferred to
  [#796](https://github.com/randomparity/kdive/issues/796).
- Any change to the capture or build write paths, the `artifacts` schema, or config/env.

## Design

A new MCP tool on the artifacts plane:

```
artifacts.fetch_raw(run_id: str, asset: "vmcore" | "vmlinux") -> ToolResponse
```

### Resolution (owner-addressed)

The `asset` enum is the egress allow-list — only these two raw kinds are ever resolvable.

- `vmlinux` → `run.debuginfo_ref` (the per-Run vmlinux object key; set by both the server and
  external build paths).
- `vmcore` → `raw_vmcore_key(conn, run.system_id)` (the existing raw-core resolver in
  `db/artifact_queries.py`; already excludes the `-redacted` sibling). `run.system_id` is the System
  the Run booted.

Owner-addressing (not `artifact_id`) is required because the server build path registers **no**
`artifacts` row for vmlinux — it carries it only as `runs.debuginfo_ref` — so id-addressing would
silently fail server builds.

### Authorization

1. Load the Run. If absent or `run.project ∉ ctx.projects`, return a not-found-shaped response
   (existence masked — the cross-project boundary).
2. `require_role(ctx, run.project, Role.CONTRIBUTOR)` (a sub-`contributor` member is denied and the
   denial is audited via the existing `RoleDenied` path).
3. For `asset == "vmcore"`: resolve the System and independently confirm
   `system.project ∈ ctx.projects` before resolving its core (defense in depth — the System and Run
   projects should match, but the vmcore boundary is checked on its own owner).

### Output

- `HEAD` the resolved object to confirm it exists and read its size; if absent, return a
  config-error with a reason (`vmcore_unavailable` / `vmlinux_unavailable`) — the agent is a member
  and may know its own asset has not been produced yet (non-debug build, no crash captured).
- Mint `presign_get(key, expires_in=KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS)` → `refs["download_uri"]`.
- `data` carries `asset`, `size_bytes`, and `ttl`. **No inline bytes** — these are multi-GB
  binaries.
- Record an audit event (`tool="artifacts.fetch_raw"`, `object_kind="runs"`, `object_id=run.id`,
  `project=run.project`) — a raw-`SENSITIVE` download is security-relevant.

### What stays unchanged

- The `REDACTED`-only gate on `artifacts.get` / `artifacts.search_text` / `artifacts.list`.
- The `SecretRegistry` / `Redactor` machinery (inline console/gdb/OTel redaction). Secrets are
  by-reference values never stored as vmcore/vmlinux artifacts, so this egress cannot expose them.
- The capture and build write paths; the `artifacts` schema; config and env.

## Lifecycle & multiplicity

Object keys are `{tenant}/{owner_kind}/{owner_id}/{name}`, so the owner scope is baked into the key
and rows accumulate (the `artifacts` table has no unique constraint on `object_key`). This is the
fix pattern ADR-0235 used for console evidence. Over one investigation:

| Asset | Key | Scope | Multiplicity |
|---|---|---|---|
| vmlinux | `…/runs/{run_id}/vmlinux` | per-Run | Every Run keeps its own; never overwritten. |
| raw vmcore | `…/systems/{system_id}/vmcore-{method}` | per-System | One immutable core per System; a second capture on the same System is **refused, not overwritten** (`vmcore.py` method-match guard). Each System keeps its own. |

The fetch tool returns the right artifact under this model: `vmlinux` resolves the exact Run's
per-Run object, and `vmcore` resolves the core of the System that Run booted. Two Runs on two
Systems yield two independently fetchable cores. The one nuance: if two Runs boot the **same**
System they resolve to that System's single shared core (correct — there is genuinely one core
there). Retaining a distinct core per crashing Run on a reused System is the per-Run capture
enhancement deferred to #796; when it lands, the `vmcore` branch addresses the core by `run_id`
directly.

## Acceptance mapping

- *Member + `contributor` downloads raw vmcore + vmlinux* → `artifacts.fetch_raw` returns presigned
  URLs for both assets of an owned Run.
- *Other project cannot fetch* → non-member Run lookup returns not-found; the vmcore branch also
  re-checks the System's project.
- *Platform-secret redaction unaffected* → the redaction machinery is not on this path and secrets
  are never these artifacts.

## Test plan (behavior + edges)

- Happy path: `contributor` fetches `vmcore` and `vmlinux` for an owned Run → presigned URL,
  `size_bytes`, no inline bytes.
- Cross-project: a member of project B requesting project A's Run → not-found-shaped (no URL, no
  existence disclosure).
- Role: a `viewer` (sub-`contributor`) member → audited role denial.
- Missing asset in own project: Run with no `debuginfo_ref` → `vmlinux_unavailable`; System with no
  raw core → `vmcore_unavailable`.
- vmcore branch with `run.system_id` NULL (no System bound) → `vmcore_unavailable`.
- Store outage on `HEAD`/presign → categorized failure envelope (not a leak, not a crash).
- The `REDACTED`-only tools are unchanged: existing `artifacts.get`/`search_text` tests stay green.

## Security considerations

- The egress allow-list is the closed `asset` enum; widening it is a reviewed code change.
- Cross-project isolation is enforced at the owner (`run.project`, and independently `system.project`
  for vmcore), mirroring the existing `_authorized_redacted_artifact` pattern.
- Presigned URLs are short-lived (`KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS`) and minted only after an
  existence `HEAD`, so the tool never hands out a URL to a missing object.
- Every successful egress is audited.
