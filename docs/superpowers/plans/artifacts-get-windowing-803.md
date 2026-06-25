# Plan: `artifacts.get` byte windowing (#803)

Spec: [docs/superpowers/specs/2026-06-25-artifacts-get-windowing-803.md](../specs/2026-06-25-artifacts-get-windowing-803.md)
ADR: [ADR-0247](../../adr/0247-artifacts-get-byte-windowing.md)

This is a single tightly-coupled change to one handler + its FastMCP registrar +
the test module + the generated tool reference. Implement directly (no subagent
fan-out). Strict TDD: write the failing test, confirm it fails for the right
reason, then the minimal implementation.

Owned file scope (do not edit other files):
- `src/kdive/mcp/tools/catalog/artifacts/reads.py`
- `src/kdive/mcp/tools/catalog/artifacts/registrar.py`
- `tests/mcp/catalog/test_artifacts_tools.py`
- `docs/guide/reference/artifacts.md` (generated — regenerate with `just docs`,
  do not hand-edit)

Guardrails (run before every commit): `just lint`, `just type`, and the focused
test module
`uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q`. Run the
full `just test` once before the first push. Doc guards: `just docs-check`,
`just docs-links`, `just adr-status-check`.

## Task 1 — Handler: add byte-window slicing to `_artifact_content` / `artifacts_get`

Where it fits: `artifacts_get` (`reads.py:206`) calls `_artifact_content`
(`reads.py:237`) which returns the `data` dict. Today `_artifact_content` returns
the whole object inline (≤ inline cap) or `content_omitted` (above it). Thread a
byte window through.

Changes in `reads.py`:

1. Add a module constant near `_MAX_SEARCHABLE_ARTIFACT_BYTES` (line 45):
   `_MAX_WINDOWED_FETCH_BYTES = 1024 * 1024` (the fetch ceiling — the largest
   object we pull whole into memory to slice; equal to the search ceiling) and the
   window-default/max constants:
   `ARTIFACT_GET_WINDOW_DEFAULT_BYTES = 16 * 1024` and
   `ARTIFACT_GET_WINDOW_MAX_BYTES = 64 * 1024`. Export the latter two for the
   registrar's `Field` bounds.
2. `artifacts_get(...)` gains keyword-only `byte_offset: int = 0` and
   `max_bytes: int = ARTIFACT_GET_WINDOW_DEFAULT_BYTES`; pass them to
   `_artifact_content`.
3. `_artifact_content(key, store_factory, refs, *, byte_offset, max_bytes)`:
   - `inline_cap = config.require(ARTIFACT_INLINE_MAX_BYTES)` (unchanged read).
   - `effective_max = min(max_bytes, inline_cap)`.
   - Replace the `head.size_bytes > inline_cap` omit test with
     `head.size_bytes > _MAX_WINDOWED_FETCH_BYTES` (the over-ceiling omit branch is
     otherwise unchanged: `{size_bytes, content_omitted: "artifact_too_large"}`,
     `download_uri` already minted).
   - In-ceiling branch (after the `fetched.sensitivity is REDACTED` recheck):
     `window = fetched.data[byte_offset : byte_offset + effective_max]`.
     `truncated = byte_offset + len(window) < head.size_bytes`.
     Build the dict: `size_bytes`, `content = window.decode("utf-8",
     errors="replace")`, `content_truncated = str(truncated).lower()`, and
     `next_offset = str(byte_offset + len(window))` **only when** `truncated`.
   - Keep `data[...]` values as `str` (the return type stays `dict[str, str] |
     None`).
4. Update the `artifacts_get` and `_artifact_content` docstrings to describe the
   window.

TDD order (each test fails first against the current handler, then passes):
- default window: a >16 KiB (but ≤ ceiling) object returns ≤ 16 KiB content,
  `content_truncated="true"`, `next_offset="16384"` (criterion 1).
- explicit window + full paging loop: drive `byte_offset`/`max_bytes`, follow
  `next_offset` to the end; assert each window's bytes concatenate to the source
  and the last has no `next_offset` and `content_truncated="false"` (criterion 2).
- `byte_offset == size` and `byte_offset > size` → empty content,
  `content_truncated="false"`, no `next_offset` (criterion 3).
- multi-byte UTF-8 split: object is `"é"*N` (2-byte chars); a `max_bytes` landing
  mid-char decodes with a replacement char, no exception (criterion 4).
- lowered configured cap clamp: monkeypatch/env `KDIVE_ARTIFACT_INLINE_MAX_BYTES`
  to 8 KiB, request `max_bytes=64 KiB` on a 32 KiB object → content is 8 KiB,
  `content_truncated="true"` (criterion 6).
- over-ceiling with window set: `size = 1 MiB + 1`, pass `byte_offset=10,
  max_bytes=100` → `content_omitted="artifact_too_large"`, `download_uri` present,
  `store.got is False` (criterion 7).
- object exactly at ceiling (1 MiB) is windowed, not omitted (edge).
- whole-object default path for a small object still returns the full content with
  `content_truncated="false"` (regression — update the existing
  `test_artifacts_get_inlines_small_redacted_content` expectations only if the
  envelope keys changed; the small-object response keys are unchanged).

Rollback: revert `reads.py`; the handler returns to whole-object inline.

Acceptance: the new tests pass; the existing `artifacts_get` tests pass after the
one expectation update below (Task 3); `just lint`/`just type` clean.

## Task 2 — Registrar: advertise `byte_offset`/`max_bytes` on the `artifacts.get` tool

Where it fits: `_register_artifacts_get` (`registrar.py:85`). The FastMCP tool
wrapper `artifacts_get` currently takes only `artifact_id`.

Changes in `registrar.py`:
1. Import `ARTIFACT_GET_WINDOW_DEFAULT_BYTES` and `ARTIFACT_GET_WINDOW_MAX_BYTES`
   from `artifact_reads`.
2. Add two `Annotated` params to the wrapper, mirroring the `search_text`
   range-stating style:
   - `byte_offset: Annotated[int, Field(ge=0, description="Start byte of the
     inline window (0-based). Page with the returned data.next_offset.")] = 0`
   - `max_bytes: Annotated[int, Field(ge=1, le=ARTIFACT_GET_WINDOW_MAX_BYTES,
     description="Max inline window bytes (1–65536); default 16384, sized to the
     tool-result token budget. Larger objects: use refs.download_uri.")] =
     ARTIFACT_GET_WINDOW_DEFAULT_BYTES`
3. Pass them through to `artifact_reads.artifacts_get(...)`.
4. Update the tool docstring to mention windowing + `data.next_offset` /
   `data.content_truncated`.

TDD: a schema test (mirroring `test_search_text_schema_advertises_context_caps`)
asserts the built app's `artifacts.get` parameter schema carries
`byte_offset.minimum == 0`, `max_bytes.minimum == 1`, `max_bytes.maximum ==
65536`, and that requesting `max_bytes=65537` is rejected at arg-binding
(criterion 5 / 10). Use the existing DB-free `build_app` pattern
(`_search_text_param_schema`) for the schema half; for the arg-binding rejection,
construct via the FastMCP tool or assert the schema `maximum` (the binding
rejection is FastMCP/pydantic-enforced — asserting the advertised `maximum`
suffices for the boundary criterion, matching how `search_text`'s caps are
tested).

Rollback: revert `registrar.py`; the tool returns to `artifact_id`-only.

Acceptance: schema test passes; `just lint`/`just type` clean.

## Task 3 — Update the one existing oversize test + regenerate the tool reference

1. `test_artifacts_get_omits_oversized_content_keeps_uri` (`reads.py` test, ~line
   455) uses `size=64*1024+1` and expects `content_omitted`. Under the new ceiling
   that size is now windowed, so this test must move its size above the new
   ceiling: `size=_MAX_WINDOWED_FETCH_BYTES + 1` and supply `data` large enough
   that, if it *were* fetched, it could slice — but assert `store.got is False`
   (omitted before fetch). Keep the `download_uri` assertion.
   Also update `test_artifacts_get_oversized_honors_head_redaction_gate` (~line
   506) which uses `size=64*1024+1` for the same reason → bump above the ceiling.
2. `test_artifacts_get_inlines_small_redacted_content` (~line 436): the small
   object still returns full content + `content_truncated="false"`; assert no
   `next_offset` key. Add the assertion; no behavior change for small objects.
3. Run `just docs` to regenerate `docs/guide/reference/artifacts.md`; verify
   `just docs-check` is clean. Commit the regenerated reference with the registrar
   change.

Acceptance: full `tests/mcp/catalog/test_artifacts_tools.py` green; `just
docs-check` clean.

## Task 4 — Full guardrails + branch review

1. `just lint && just type && just test` (full suite once).
2. `just docs-check && just docs-links && just adr-status-check`.
3. Adversarial branch review loop (`/challenge --base main`), address findings.

Commit boundaries (small, logically scoped, do not squash):
- (already committed) spec + ADR + README index.
- handler windowing (Task 1) + its tests.
- registrar schema (Task 2) + schema test + regenerated tool reference (Task 3).
- any review-fix commits.
