# Provisioning-for-debugging agent guides — Implementation Plan (#955)

> **Execution mode:** tasks are tightly coupled (the three doc edits and the snapshot
> regeneration must land together or the completeness/drift guards fail), so implement
> directly in this session, not via parallel subagents. Steps use checkbox syntax.

**Goal:** Document, in the agent-facing workflow guides, the provision-bound
debug/live-introspection knobs (`debug.gdbstub`, `debug.preserve_on_crash`,
`ssh_credential_ref`) so an agent chooses them at `systems.provision` instead of paying a
teardown → reprovision → rebuild → reboot cycle when it discovers the need mid-session.

**Spec:** `docs/specs/2026-07-02-provisioning-for-debugging-docs-955.md`.
**Governing ADR:** ADR-0284 (agent-facing doc system). Content addition; no new ADR.

## Global constraints

- **Docs only.** No source behavior, tool, schema, or data-contract change.
- **No ADR/issue refs in served doc bodies.** ADR-0270: agent-facing surfaces (the served
  `agent-index.md` and toolset docs are read over MCP) must not cite `ADR-NNNN` or `#NNNN`.
  Write plain factual prose; avoid "critical", "crucial", "essential", "significant",
  "comprehensive", "robust", "elegant"; use "Milestone" not "Sprint".
- **Completeness-guard wording** (`tests/mcp/resources/test_toolset_doc_completeness.py`):
  a served toolset doc must name exactly its namespace's live tools. It greps
  `\b<ns>\.[a-z_]+`, so a profile field written as `debug.gdbstub` in `debug.md`/`systems.md`
  is mis-read as a stale `debug.*` tool. Write it as the profile's `debug` section with
  `gdbstub: true` / `preserve_on_crash: true`. In `agent-index.md`, backticked `ns.tool`
  tokens must be live tools (`test_agent_index_references_only_live_tools`); the full
  profile path `provider.local-libvirt.debug.gdbstub` is safe there.
- **Snapshot drift.** After editing canonical docs under `docs/`, regenerate the served
  snapshots with `just resources-docs`; `just resources-docs-check` gates drift in CI.
- **Guardrails before commit:** `just lint`, `just type`, `./scripts/check-doc-links.sh`,
  `./scripts/check-doc-paths.sh`, `just resources-docs-check`, and the focused doc tests.
- Commit messages: Conventional Commits, imperative ≤72 chars, trailer
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Files touched

- `docs/guide/agent-index.md` — add the provisioning-for-debugging section.
- `docs/guide/toolsets/debug.md` — add a provision-bound gdbstub note.
- `docs/guide/toolsets/systems.md` — add a provision-bound debug/live-ssh note.
- `src/kdive/mcp/resources/_content/agent-index.md`, `…/toolsets-debug.md`,
  `…/toolsets-systems.md` — regenerated snapshots (never hand-edited).

## Tasks

### Task 1 — Add the index section to `agent-index.md`

- [ ] Add a `## Provisioning for debugging and live introspection` section immediately
  after "The typical session" list.
- [ ] Content: state the rule — these are bound at `systems.provision` and cannot be added
  to a ready System; decide before provisioning or reprovision (which rebuilds and
  reboots). List the three knobs as a short bullet list:
  - `provider.local-libvirt.debug.gdbstub: true` — required before `debug.start_session`
    can attach a live GDB session; otherwise start_session fails and you must reprovision.
  - `provider.local-libvirt.debug.preserve_on_crash: true` — holds a crashed guest
    (vCPUs stopped) for live post-crash attach instead of destroying it.
  - `provider.local-libvirt.ssh_credential_ref` — the guest credential the drgn-over-SSH
    live introspection transport (`introspect.run`) needs to reach the guest; necessary
    but not sufficient — live introspection also needs a drgn-capable image and an
    SSH-reachable guest.
- [ ] Cross-reference `systems.reprovision` as the (expensive) remedy if deferred.
- **Acceptance:** section present; `test_agent_index_references_only_live_tools` passes
  (only live `ns.tool` backticks: `debug.start_session`, `systems.provision`,
  `systems.reprovision`, `introspect.run`); no `ADR-NNNN`/`#NNNN` in the body.

### Task 2 — Add the provision-bound note to `debug.md`

- [ ] Add a short note (near the session lifecycle) that a live GDB session requires the
  System to have been provisioned with the profile's `debug` section `gdbstub: true`;
  otherwise `debug.start_session` fails with a `configuration_error` directing you to
  reprovision with gdbstub set. Paraphrase — do not quote the verbatim source string.
- [ ] Point back to the index section's provisioning guidance.
- **Acceptance:** `test_each_served_toolset_doc_names_exactly_its_namespace_tools` passes
  (no dotted `debug.<nonword>` token; `gdbstub`/`preserve_on_crash` written bare).

### Task 3 — Add the provision-bound note to `systems.md`

- [ ] Add a short note in the "Defining and provisioning" subsection that the profile's
  `debug` flags and the live-ssh credential are bound at provision and cannot be added to a
  ready System — set them before `systems.provision`, or use `systems.reprovision`.
- **Acceptance:** `test_each_served_toolset_doc_names_exactly_its_namespace_tools` passes
  for `systems` (only live `systems.*` tokens; already-named tools unchanged).

### Task 4 — Add a content-presence guard (TDD)

The completeness and snapshot guards prove tool-name and snapshot-vs-canonical sync only —
neither asserts the provisioning-for-debugging guidance exists. Without a guard, a later
edit can silently delete the issue's whole deliverable and CI stays green. Add a focused
test asserting the served `agent-index.md` snapshot carries the section and its knobs.

- [ ] Write the failing test first (in a new
  `tests/mcp/resources/test_provisioning_for_debugging_docs.py` or alongside the
  completeness test): read the served `agent-index` snapshot via the `DOC_RESOURCES`
  entry (not a hard-coded path) and assert it contains a provisioning-for-debugging
  heading and names `gdbstub`, `preserve_on_crash`, and `ssh_credential_ref`. Confirm it
  fails before Task 1's content lands (or reordered: run it after edits and confirm it was
  red on the pre-edit snapshot).
- [ ] Keep the assertion behavioral (keywords the agent must see), not brittle
  (no exact-sentence match).
- **Acceptance:** the new test passes on the edited snapshot; `ty`/`ruff` clean on it.

### Task 5 — Regenerate snapshots and run guardrails

- [ ] `just resources-docs` to regenerate `_content/*` snapshots; review the diff (it
  writes every allowlisted snapshot — confirm only the three intended files changed).
- [ ] `just resources-docs-check` → clean.
- [ ] `./scripts/check-doc-links.sh` and `./scripts/check-doc-paths.sh` → clean.
- [ ] Focused tests: `uv run python -m pytest tests/mcp/resources/test_toolset_doc_completeness.py tests/mcp/resources/test_provisioning_for_debugging_docs.py tests/mcp/core/test_no_adr_leak.py -q`.
- [ ] `just lint` and `just type` (whole tree) clean.
- [ ] Commit canonical docs + regenerated snapshots + the new test together (one logical change).

## Rollback

Revert the branch. No migration, schema, or config to unwind; docs-only.

## Verification the change works

The served docs are read over MCP as `resource://kdive/docs/guide/agent-index.md` etc.
The completeness and snapshot guards prove tool-name and snapshot-vs-canonical sync; the
Task 4 content-presence guard is the automated proof the provisioning-for-debugging
guidance is actually present in the served index. A human reading the rendered index sees
the provisioning-for-debugging section before the debug stage — the discovery gap the
issue names is closed at the workflow-map level and reinforced in the two toolset guides
the agent lands on.
