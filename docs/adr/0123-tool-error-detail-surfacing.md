# ADR 0123 — Tool-error detail surfacing on the response envelope

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers

## Context

The uniform tool-response envelope (ADR-0019, `ToolResponse` in `src/kdive/mcp/responses.py`)
carries `error_category` but no human-readable reason. ADR-0019 deliberately constrained the
envelope to "references, never dumps" to keep it JSON-trivial. In practice this discards
information the server already computed:

- `CategorizedError(message, category, details)` (`src/kdive/domain/errors.py:57-82`) passes
  `message` to `Exception.__init__`, but `ToolResponse.failure_from_error`
  (`responses.py:194-210`) extracts only `exc.details` and drops `str(exc)`.
- `ProvisioningProfile.parse()` attaches Pydantic's structured `errors()` to `details["errors"]`
  (`provisioning.py:283-287`), but `_safe_error_details` (`responses.py` and the duplicate in
  `admission.py:120-129`) filters `data` to scalars, and `errors` is a list — so it is dropped.

The result (observed in MCP-surface testing): a `configuration_error` returns with empty `data`
and no message, leaving the caller unable to learn what was wrong. This blocked end-to-end
onboarding. See `../design/mcp-onboarding-error-ergonomics.md`.

## Decision

We will add a `detail: str | None` field to `ToolResponse`, populated from the
`CategorizedError` message, and widen the data-detail filter to preserve one reserved nested
`errors: list[{loc, msg, type}]` key (bounded to 20 entries, each sanitized to scalars). The
duplicated `_safe_error_details` is consolidated to one helper, and `AdmissionFailure` gains a
`detail` field threaded through the systems mapper so both the generic and the admission seams
surface the reason. The no-leak guard is enforced **at the seam**, not at the raise site: the
seam holds a closed set of suppressed categories (`authorization_denied`, `not_found`) for which
`detail` is a fixed constant and `str(exc)` is ignored, so no raise site — including
`ProjectMembershipDenied`, whose message embeds the named project — can leak through `detail`.
Because `detail` is a new client egress, messages surfaced for the diagnostic categories must be
author-controlled and must not interpolate secrets, secret-ref paths, internal hostnames, or
object-store keys; raise sites that violate this are fixed in the same change. No automatic
redaction pass is added.

## Consequences

- Every rejected tool call becomes debuggable from the wire alone; the profile wall (finding 1)
  is unblocked even before its own work item lands.
- The advertised output schema is unchanged (stays flat per ADR-0113); `detail` is an additive
  wire field, and `errors` rides the already-recursive `data` payload.
- New obligation: a no-leak regression test must assert non-member/`not_found` denials carry the
  seam's constant `detail` with no resource name — driven through a raise site whose message *does*
  embed the name, so the test proves the seam (not the raiser) enforces it.
- New obligation: a one-time audit of `CategorizedError` raise sites for the diagnostic categories
  to confirm none interpolate secrets/paths into the now-surfaced message.
- This refines ADR-0019's "references, never dumps" rule: a bounded, sanitized reason is now
  allowed because an opaque category proved un-actionable.

## Alternatives considered

- **Structured `errors` in `data` only, no `detail` field** (honors ADR-0019 unchanged): clients
  must parse a list to render any message; rejected because a one-line reason is the single most
  useful thing and costs one nullable field.
- **`detail` only, no structured `errors`**: humans get a message but agents lose machine-readable
  field paths that point at the exact bad key; rejected — the structured list is already computed
  at the throw site, dropping it is pure loss.
- **Echo the raw exception string everywhere, including denials**: simplest, but leaks resource
  existence on the no-leak path; rejected.
