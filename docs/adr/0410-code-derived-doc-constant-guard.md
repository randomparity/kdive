# ADR-0410: guard code-derived doc constants against their source of truth (#1368)

- Status: Accepted
- Date: 2026-07-21

## Context

The agent-surface review (epic #1360) found doc prose restating values whose real source of
truth is a Python constant, hand-copied and silently stale. `agent-index.md` claimed the
catalog held "~100 tools" while the live registry had grown to ~140. The artifacts upload
docstrings cite "the 5 GiB single-PUT size limit" — a figure that is really
`min(SINGLE_PUT_MAX_BYTES, KDIVE_MAX_UPLOAD_BYTES)`, with no check tying the sentence to
either constant.

Existing generators (`gen_tool_reference`, `gen_config_reference`, `gen_doc_resources`)
regenerate whole docs from the registry and diff the committed copy (`*-docs-check`), but no
guard covered these two point-constants embedded in otherwise hand-authored prose. The
`postmortem.crash` verb list is already effectively covered — `vmcore/registrar.py` joins
`CRASH_COMMAND_ALLOWLIST` into the docstring at import, rendered into the reference and gated
by `docs-check` — so it is out of scope here.

## Decision

Add `scripts/gen_doc_constants.py` (`just doc-constants` / `just doc-constants-check`),
modeled on `resources-docs-check`: each constant is a `Binding` whose `expected` value is
computed from source and matched against a committed occurrence via a regex with one capture
group. `--check` fails when any committed value disagrees; it is added to the `ci` umbrella
and, because CI invokes recipes individually, listed as its own `ci.yml` step.

Two binding kinds, split by whether the surrounding text is authored:

1. **Generated** (writable) — the approximate tool count in `agent-index.md`, a pure derived
   number. The live registry count (`gen_tool_reference._registry_tools`) is rounded to the
   nearest ten; `just doc-constants` rewrites the `~NNN` in place. `resources-docs` then
   re-mirrors the served snapshot, so the existing `resources-docs-check` stacks on top.
2. **Guarded** (not writable) — the "N GiB single-PUT size limit" figure in the artifacts
   registrar docstrings, embedded in a sentence carrying nuance ("not a way to beat the
   clock"). A generator must not rewrite hand-authored source docstrings (the repo's
   generators only ever write `docs/`), so `--check` asserts the figure equals the
   source-derived `min` of the S3 cap and the policy limit and names the value to edit in on
   drift, rather than rewriting the `.py`.

The effective ceiling is rendered as an exact-GiB string; a non-GiB-multiple constant raises
rather than emitting a rounded figure.

## Consequences

A change to `SINGLE_PUT_MAX_BYTES`/`KDIVE_MAX_UPLOAD_BYTES` or a tool-count crossing a
rounding-of-ten boundary now fails `doc-constants-check` until the doc is resynced
(`just doc-constants` for the generated count; a one-line docstring edit for the guarded
ceiling, then `just docs` to regenerate the downstream reference page). The guard covers only
the two reviewed constants; a newly hand-copied constant is not auto-detected until it is
added as a `Binding`. The asymmetry (write vs guard) is deliberate — it keeps machine
generation off hand-authored source docstrings. Guard, generator, and docs only; no schema
change, no migration.
