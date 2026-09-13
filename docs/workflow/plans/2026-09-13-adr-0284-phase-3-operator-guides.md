# Implement ADR-0284 Phase 3 operator guides

Goal: complete the existing ADR's role-gated operator documentation phase and accept the ADR
when the documented resource surface is live. The implementation adds only static Markdown,
literal doc-resource entries, generated snapshots, and tests; existing middleware continues to
apply the platform-role gate.

Tech stack: Python resource registry, Markdown canonical sources, generated package snapshots,
pytest, and `just` guardrails.

## Global Constraints

- Preserve the frozen #2299/P3 exclusions: no auth middleware redesign, non-doc features, or
  Phase 2 guide edits except required index integration.
- Use the live `kdive.mcp.exposure._TOOL_SCOPES` namespace surface as the coverage source.
- Every new operator resource has `audience="operator"` and a fixed literal `resource://` URI.
- Regenerate snapshots with `just resources-docs`; do not hand-copy them.
- Run `just lint`, `just type`, `just test`, and `just ci` before delivery.

Expected implementation size: 420–560 changed lines (M) — eight concise guides, registrations,
generated snapshots, and focused resource tests.

## File map

- Create `docs/guide/agent-index-operator.md` and seven `docs/guide/toolsets/*.md` operator
  guides for `accounting`, `audit`, `inventory`, `ops`, `reports`, `secrets`, and `shapes`.
- Modify `src/kdive/mcp/resources/registrar.py` to register these exact operator resources.
- Regenerate matching files in `src/kdive/mcp/resources/_content/`.
- Modify `tests/mcp/resources/test_doc_resources.py` and `tests/mcp/resources/test_doc_exposure.py`
  for registration, completeness, and role-gated exposure contracts.
- Modify `docs/README.md` to list both served workflow indexes.
- Modify `docs/adr/0284-agent-facing-workflow-docs.md` only to change its status after Phase 3
  contracts pass.

## Task 1 — Define and register the operator resource set

**Interfaces:** consumes `DocResource`; provides eight fixed `DOC_RESOURCES` entries to the
registrar and `audience_by_uri`.

**Verification:**

- Contract: each new URI is literal, has `audience="operator"`, and is read from its packaged
  snapshot. Mode: focused-test. Red observation: the exact resource-set assertion fails before
  entries exist. Green command: `just test-verbose tests/mcp/resources/test_doc_resources.py`.

Steps:

1. Write the canonical operator index and seven purpose guides from the coverage table in the
   design, naming every registered tool and its role boundary.
2. Add the eight fixed registrations with matching source and snapshot names and
   `audience="operator"`.
3. Add a resource test that pins the derived set, audience, literal URIs, read-back, and per-guide
   live namespace completeness; run the focused test and expect green.

Acceptance: all seven live elevated namespaces without existing investigation guides have one
operator purpose guide; the index links only to those served operator guides.

## Task 2 — Prove role-gated exposure and snapshot drift protection

**Interfaces:** consumes `DOC_RESOURCES` and existing `DocExposureMiddleware`; provides tests
that exercise the real registered operator index rather than a synthetic URI.

**Verification:**

- Contract: callers with platform roles list and read the registered operator index; callers
  without one neither list nor read it. Mode: focused-test. Red observation: a registered index
  is absent or ungated before registration. Green command:
  `just test-verbose tests/mcp/resources/test_doc_exposure.py`.
- Contract: canonical Markdown and packaged snapshots match. Mode: focused-test. Red
  observation: pre-generation snapshot check reports a missing or stale snapshot. Green command:
  `just resources-docs-check`.

Steps:

1. Replace synthetic-only assertions with coverage of the actual registered operator index and
   operator resource set while retaining the existing fail-closed middleware cases.
2. Add the operator-index pointer to `docs/README.md` so its served-doc inventory is accurate.
3. Run `just resources-docs` and inspect generated snapshot paths, then run
   `just resources-docs-check`.
4. Run both focused test modules and expect all selected tests to pass.

Acceptance: the gates are demonstrated against a real operator resource and every generated
snapshot matches canonical source.

## Task 3 — Accept the completed ADR and run repository verification

**Interfaces:** consumes the passing resource and exposure contracts; changes only ADR-0284's
status line from Proposed to Accepted.

**Verification:**

- Contract: ADR status is valid and Phase 3 artifacts are complete. Mode: focused-test. Red
  observation: `just adr-status-check` rejects an invalid status or record shape. Green command:
  `just adr-status-check`.

Steps:

1. Re-read the diff against `main`; verify the exact coverage table and no unrelated guide or
   middleware change.
2. Change ADR-0284's status to `Accepted`.
3. Run `just adr-status-check`, then `just lint`, `just type`, `just test`, and `just ci`.

Acceptance: ADR-0284 states Accepted only after the role-gated index, all seven derived guides,
their registrations and snapshots, and focused coverage are all present.
