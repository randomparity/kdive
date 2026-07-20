# 0394 — Upload responses state the full deadline contract, locally

Status: Accepted

## Context

The `artifacts.create_run_upload` / `artifacts.create_system_upload` success
responses hand an agent a bare relative `expires_in` (the presign TTL) with no
reference clock, do not surface the reaper-enforced manifest deadline, and name
no recovery action. This violates the five-part limit doctrine in AGENTS.md and
was observed to make an agent misuse chunked upload as a time workaround (#1336).

Two design questions have viable alternatives worth recording:

1. **Where does the reference clock (`server_time`) live** — in the shared
   `ToolResponse` envelope (one mechanism, every tool) or local to the
   deadline-bearing upload responses?
2. **Two deadlines exist** (the per-URL presign expiry and the reaper's manifest
   deadline). Surface one, or both?

## Decision

**1. Keep the deadline contract local to the upload responses; do not extend the
shared envelope.** `server_time`, absolute `expires_at` (per item),
`manifest_deadline`, and `on_expiry` are carried in `data`, not as new
`ToolResponse` fields. `ToolResponse` is `extra="forbid"` and every tool test
round-trips exact envelopes, so a new envelope field is a surface-wide change
(and a standing invariant every future tool must satisfy) for a need only
deadline-bearing responses have. Only the upload tools return a deadline today;
promoting `server_time` to the envelope is deferred until a second deadline-
bearing surface exists (rule of three).

**2. Surface both deadlines, with distinct scope.** The per-URL presign expiry
(`data.expires_at` on each item, plus the existing relative `expires_in`) is the
"begin the PUT by" wall for that URL; the manifest deadline
(`data.manifest_deadline`, collection level) is the reaper's reclaim window for
the whole upload. Both are reported, each documented for what it governs. They
coincide when `UPLOAD_TTL_SECONDS ≤ 3600` and diverge above it.

**3. One authoritative clock.** `replace_manifest` returns `(server_time,
deadline)` via `RETURNING now(), deadline`, both from the same transaction, so
`manifest_deadline − server_time == ttl` exactly and the agent measures against
the reaper's clock. Each item's `expires_at` is `server_time + presign_ttl`
(conservative: transaction start slightly precedes the boto3 signing instant).

**4. Recovery is a structured hint, not a linear next action.**
`data.on_expiry = {tool, effect}` names the re-mint tool and states that re-mint
resets the deadline. `suggested_next_actions` stays the happy path
(`complete_build` / `provision_defined`); re-mint is conditional recovery, and
putting it in the linear list would wrongly imply an immediate re-mint on a
successful mint.

**5. Descriptions state scope and the non-constraint.** Both wrapper docstrings
(the agent-facing contract, serialized into the generated reference) state the
begin-before-`expires_at` scope, the in-flight-not-interrupted clause, the
manifest-deadline/`server_time` pair, the re-mint recovery, and that `chunks`
are for objects over the 5 GiB size limit, not for time pressure.

## Consequences

- The upload responses satisfy the five-part limit contract; an agent can
  compute remaining time and knows the recovery action, removing the incentive
  to invent time workarounds.
- `replace_manifest` gains a return value (its sole caller is the upload path);
  `_upload_response` and the collection builder take `server_time` /
  `manifest_deadline`.
- The generated tool reference changes, so `just docs` must run and `docs-check`
  gates it.
- A future deadline-bearing tool that wants the same clock will duplicate a few
  lines until the third case justifies promoting `server_time` to the envelope;
  that promotion is a later ADR, not this one.
- **Precondition — per-URL `expires_at` spans two clocks.** `manifest_deadline`
  is measured and enforced entirely on the Postgres clock (the reaper reads the
  same `deadline`), so it is exact. `expires_at`, by contrast, is rendered on the
  DB clock (`server_time + presign_ttl`) but enforced by the object store on
  *its* clock — `PresignedUpload` carries no absolute expiry to report the real
  signed window. This is sound only while the DB and object-store clocks are
  roughly aligned (both NTP-synced), which kdive already assumes operationally.
  The skew direction is safe: `server_time` is the transaction start, which
  precedes the signing instant (and any same-owner advisory-lock wait), so
  `expires_at` can only be *understated* — the worst case is a needless re-mint,
  never a lapsed URL trusted as live. Treat `manifest_deadline`, not
  `expires_at`, as the authoritative reaper-enforced wall.

## Considered & rejected

- **`server_time` in the shared `ToolResponse` envelope.** One mechanism, but a
  surface-wide field (and permanent invariant) with `extra="forbid"` churn
  across every tool test, for a one-surface need. Premature; revisit at the
  third deadline-bearing surface.
- **Surface only the manifest deadline (drop the presign expiry).** Loses the
  real "start your PUT by" wall for each URL; an agent that reasons only about
  the manifest deadline could let a presigned URL lapse and get a `403`.
- **Surface only the presign TTL (status quo, made absolute).** Still hides the
  reaper's actual contract — gap 2 unaddressed.
- **Re-mint tool in `suggested_next_actions`.** Pollutes the linear happy path
  on a success and reads as "re-mint now"; the conditional `on_expiry` hint is
  more precise.
- **Compute `server_time` in Python (`datetime.now(UTC)`).** A second clock that
  can disagree with the DB's `now()` the reaper uses; the whole point is one
  authoritative clock.
