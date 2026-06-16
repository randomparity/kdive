# ADR 0135 — Investigation naming + reporting fields

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-16
- **Deciders:** KDIVE maintainers

## Context

`Investigation` (ADR-0026) groups Runs into a project-scoped campaign. Today it carries
a `title` (set once at `investigations.open`) and a list of `external_refs`
(`tracker`/`id`/`url`, e.g. Bugzilla or JIRA). Issue [#448](https://github.com/randomparity/kdive/issues/448)
asks for richer agent-settable naming/text "to improve reporting", citing a free-form
text field as an example.

Three gaps block that goal:

1. **No free-form text.** `title` is a short label; there is no place for the agent to
   record what the campaign is about, a hypothesis, or a status note.
2. **`title` is immutable.** It is only ever set at `open`; an agent that learns a better
   name (or mistypes one) cannot correct it.
3. **Reporting hides the data.** `investigations.get` returns only the project and the
   *count* of external refs — not the `title`, the refs themselves, or `last_run_at`. And
   there is no way to enumerate a project's investigations at all, so cross-investigation
   reporting is impossible over the tool surface.

The constraint is the cross-cutting invariants in `AGENTS.md`: a uniform `ToolResponse`
envelope, the stable `ErrorCategory` taxonomy, per-Investigation advisory-lock serialization
for mutations (already used by `link`/`unlink`), RBAC (`operator` mutates, `viewer` reads),
and ADR-0020's no-authz-`ErrorCategory` rule (denials raise).

## Decision

We will close all three gaps with one small surface, keeping the existing
`open`/`close`/`link`/`unlink` semantics intact:

1. **First-class `description` column.** Add a nullable `description text` column to
   `investigations` (migration `0037`) and a `description: str | None` field to the
   `Investigation` model, backstopped by a DB `CHECK (char_length(description) <= 4096)`.
   `investigations.open` accepts an optional `description`. **Length bounds are enforced at the
   write boundary (the `open`/`set` handlers), not as `Field` constraints on the model**, because
   `model_validate` runs on the read path (the repository deserializes every row through it): a
   retroactive field bound would make any already-persisted out-of-bound row unreadable. `title`
   was previously a bare unbounded `str` (and `''` is storable), so a field bound on it would
   break reads of existing rows; the boundary check (`1..=200` chars for `title`, `0..=4096` for
   `description`) avoids that. `description`'s column is brand-new, so its DB `CHECK` cannot
   reject any existing row and serves as defence-in-depth behind the boundary check. A
   `description` of `""` normalizes to `NULL` on both `open` and `set`.

2. **`investigations.set` (new, mutating, `operator`).** Update `title` and/or `description`
   on a **non-terminal** Investigation (`open`/`active`), under the per-Investigation advisory
   lock, audited. Partial update is **value-based, not a Python sentinel** (the transport cannot
   distinguish an omitted optional from an explicit `null` — both arrive as `None`): `None` for a
   field means *leave unchanged*; `description=""` is the *clear* signal (sets `NULL`); `title`
   cannot be cleared (`NOT NULL`) and `title=""` is a `configuration_error`. At least one field
   must be supplied. A terminal (`closed`/`abandoned`) Investigation rejects with
   `configuration_error` carrying `current_status`, matching `link`/`unlink`. `title` thereby
   becomes mutable while open.

3. **Surface the fields.** `investigations.get` (and the mutators' rendered envelope) return
   `title`, `description`, the full `external_refs` list, `state`, and `last_run_at` in
   `ToolResponse.data` — not just a ref count.

4. **`investigations.list` (new, read-only, `viewer`).** Return the caller's project-scoped
   Investigations as a collection envelope, newest-first, with an optional `state` filter.
   Scoped to the projects the caller holds `viewer` on; no cross-project leakage.

## Consequences

- **Migration `0037`** is additive and backward-compatible: a nullable column with a default
  of NULL and a `CHECK`. Existing rows read back with `description = None`. No backfill.
- **`title` is now mutable.** Any consumer that assumed `title` is write-once must not cache it
  across a `set`. Within this codebase nothing caches it; the audit log records every change.
- **Free-form text is tenant data, not provider output, so it is *not* routed through the secret
  redactor** (`security/`). The redactor exists to scrub secrets the *system* resolved and
  guest/console/gdb output; an agent's own description is its own text and redacting it would
  corrupt it. The 4096-char bound caps storage abuse; the value is parameterized in SQL (no
  injection) and returned verbatim only to a `viewer` on the owning project.
- **New tools** append to the existing `investigations.*` registrar — no entrypoint change
  (`_PLANE_REGISTRARS` is untouched; the registrar already exists).
- **Reporting payloads grow.** `get`/`list` now echo the (bounded) `description` and full refs.
  This is the intended improvement; the bound keeps the envelope size predictable.
- Follow-on: none required. `usage.investigation` (spend reporting) is unaffected.

## Alternatives considered

- **Fold free-form text into `external_refs` as a synthetic `{tracker: "note", ...}` entry.**
  Rejected: overloads a structured tracker-link list with prose, has no natural `id`/`url`, and
  makes the natural-key dedup in `link`/`unlink` meaningless for notes. A description is a
  distinct concept from a tracker link.
- **A separate `investigation_notes` table (one-to-many timestamped notes).** Rejected as
  premature (global standard: no premature abstraction). The issue asks for *a* free-form field
  to improve reporting, not a threaded note history. A single nullable column is queryable,
  reportable, and trivially editable; a notes table can supersede this in a later ADR if a real
  need for append-only history appears.
- **Keep `title` immutable; add a separate mutable `display_name`.** Rejected: two near-identical
  name fields confuse the agent and the report. `title` was immutable only incidentally (no
  setter existed), not by a stated invariant; making it editable while the Investigation is open
  is simpler and matches user intent ("agent selected naming").
- **Route `description` through the redaction registry before persistence.** Rejected: the
  registry redacts *resolved secrets and external output*, keyed by the op's resolved secret set;
  an agent-authored description has no such set, so redaction would be a no-op at best and a
  corrupting substring match at worst. Bounding length is the right control here.
- **Make `investigations.list` global (all projects) for an operator.** Rejected: it would leak
  cross-tenant campaign titles/descriptions. List is scoped to the caller's `viewer` projects,
  consistent with `resources.list`'s visibility filter.
