# Plan: End-to-end artifact retrieval through `artifacts.get` (#485)

Spec: [docs/design/artifacts-get-content-retrieval.md](../../design/artifacts-get-content-retrieval.md)
ADR: [ADR-0140](../../adr/0140-artifacts-get-content-retrieval.md)

Single-handler change, implemented directly in this session (tightly coupled —
config setting → handler → registrar doc → generated docs all move together). TDD
throughout: failing test first, minimal implementation, focused test + guardrails.

Guardrail commands (run before every commit):
- `just lint` (ruff check + format --check)
- `just type` (ty, whole tree)
- focused test: `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q`
- doc tests when docs change: `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q`
- before first push: `just ci` (full gate) + `just check-mermaid` + `just docs-links`

## Task 1 — Config settings

Add two server-scoped settings to `src/kdive/config/core_settings.py`, in the
`upload`/`artifacts` neighborhood, mirroring `MAX_UPLOAD_BYTES` / `UPLOAD_TTL_SECONDS`:

- `ARTIFACT_INLINE_MAX_BYTES` — `name="KDIVE_ARTIFACT_INLINE_MAX_BYTES"`,
  `parse=_int`, `default=str(64 * 1024)`, `processes=_SERVER`, help describing the
  inline-content cap for `artifacts.get` (anything larger uses the download URI).
- `ARTIFACT_DOWNLOAD_TTL_SECONDS` — `name="KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS"`,
  `parse=_int`, `default="900"`, `processes=_SERVER`, help describing the
  presigned download-URL TTL for `artifacts.get`.

**Files:** `src/kdive/config/core_settings.py`; regenerate the config reference
with `just config-docs` (CI gates `config-docs-check` and `env-docs-check`
individually). `tests/config/test_manifest_completeness.py` enforces every setting
is documented — run it.
**Acceptance:** `config.get(ARTIFACT_INLINE_MAX_BYTES)` and
`config.get(ARTIFACT_DOWNLOAD_TTL_SECONDS)` return the defaults;
`just config-docs-check` and `just env-docs-check` green; manifest-completeness
test green.
**Verify first:** run `test_manifest_completeness` before adding to confirm it
pins the documented setting list, then regenerate the config reference in the same
commit.

## Task 2 — Extend the `artifacts_get` handler

In `src/kdive/mcp/tools/catalog/artifacts/reads.py`:

- Move `artifacts_get` onto `ArtifactReadHandlers` (it needs the
  `search_store_factory` seam, exactly like `artifacts_search_text`), OR keep the
  module-level function and thread an optional store factory. Prefer the
  `ArtifactReadHandlers` method form so the injected-store test seam already used by
  `search_text` is reused. Keep a module-level `artifacts_get` thin wrapper if the
  registrar / tests import it directly (they do — `reads.artifacts_get` and the test
  module import it), to avoid churning unrelated call sites; the wrapper builds a
  store from env and delegates.
- Behavior, after the existing `_authorized_redacted_artifact` gate returns a key:
  1. Build the store via the factory. On `CategorizedError`, return the metadata
     envelope (`available`, `refs={"object": key}`) plus
     `data={"content_unavailable": "store_unconfigured"}`. No hard failure.
  2. `head(key)` off-thread (`asyncio.to_thread`). On `CategorizedError` or
     `head is None`, return metadata envelope + `content_unavailable: store_error`.
  3. `presign_get(key, expires_in=config.get(ARTIFACT_DOWNLOAD_TTL_SECONDS))`
     off-thread → `refs["download_uri"]`. On `CategorizedError`, fall to the
     `content_unavailable: store_error` path (no URI, no content).
  4. If `head.size_bytes > ARTIFACT_INLINE_MAX_BYTES`: set
     `data["content_omitted"] = "artifact_too_large"`, `data["size_bytes"]`, and
     return with the URI; do not call `get_artifact`.
  5. Else `get_artifact(key, head.etag)` off-thread; if
     `fetched.sensitivity is not Sensitivity.REDACTED` return a not-found-shaped
     `_config_error(artifact_id)` (the redaction gate, same as `search_text`). On
     `CategorizedError`, `content_unavailable: store_error`. Else set
     `data["content"] = fetched.data.decode("utf-8", errors="replace")`,
     `data["content_truncated"] = "false"`, `data["size_bytes"]`, return with URI.
- Reuse `Sensitivity`, `_config_error`, `asyncio.to_thread`, the `_SearchStore`
  Protocol (extend if `presign_get` is not on it — add `presign_get(self, key, *,
  expires_in) -> str`).

**Files:** `src/kdive/mcp/tools/catalog/artifacts/reads.py`.
**Conventions:** uniform `ToolResponse`; `error_category` only on failures; literal
`suggested_next_actions`; redact-before-return (the sensitivity gate IS the
redaction control here); ≤100 lines/function, complexity ≤8 — factor the
content/URI enrichment into a helper if `artifacts_get` grows past the limit.
**Acceptance:** the Task-3 tests pass; the existing `artifacts_get` tests
(redacted-returns-ref, requires-viewer, sensitive/quarantined/cross-project/
malformed not-found) stay green unchanged.

## Task 3 — Tests (write first, TDD)

In `tests/mcp/catalog/test_artifacts_tools.py`, extend the injected `_SearchStore`
fake with a `presign_get(key, *, expires_in) -> str` (record the `expires_in`) and
add cases from the spec's Tests section:

- redacted ≤inline-cap → `content` equals decoded object, `size_bytes` set,
  `download_uri` present, `content_truncated == "false"`.
- redacted >inline-cap (object size above cap) → `content` absent,
  `content_omitted == "artifact_too_large"`, `download_uri` present,
  `store.got is False`.
- fetched `sensitivity != REDACTED` → not-found-shaped `configuration_error`.
- store factory raises → metadata envelope (`status == "available"`) +
  `content_unavailable == "store_unconfigured"`, no `download_uri`.
- `head` raises / `presign_get` raises → metadata envelope +
  `content_unavailable == "store_error"`.
- existing sensitive/quarantined/cross-project/malformed/viewer cases unchanged.
- `expires_in` passed to `presign_get` equals the configured TTL (use
  `monkeypatch.setenv` or assert against the default).

Each test fails first for the expected reason (the current `artifacts_get` returns
no `data`), then passes after Task 2.

**Verify-it-catches-failure:** confirm the ≤cap test fails on the current handler
(no `content` key) before implementing.

## Task 4 — Registrar doc + generated reference

- Update the `artifacts.get` tool docstring/description in
  `src/kdive/mcp/tools/catalog/artifacts/registrar.py` to state it returns the
  redacted content inline (≤cap) or via `refs["download_uri"]`. Keep `read_only()`
  annotation and `maturity: "partial"` (the schema stays flat — ADR-0113).
- If `artifacts_get` becomes an `ArtifactReadHandlers` method, update the registrar
  call site to `read_handlers.artifacts_get(...)` (mirrors `search_text`).
- Regenerate the committed tool reference with `just docs`
  (`scripts/gen_tool_reference.py`); commit the regenerated file. CI gates
  `docs-check` individually. `tests/mcp/core/test_tool_docs.py` must stay green
  (every param documented; `artifacts.get` already mapped to this test module).

**Files:** `registrar.py`, generated tool-reference doc(s).
**Acceptance:** `test_tool_docs.py` green; generated reference reflects the new
behavior; no other tool's doc churns.

## Rollback / cleanup

- Pure-additive: new config settings (defaulted), new response `data`/`refs` keys,
  no migration, no schema change, no new store method. Reverting the branch fully
  removes the surface.
- If the generated-doc regeneration touches unrelated tools (base moved), re-run
  the generator after rebasing rather than hand-editing.

## Ordering

Task 1 → Task 3 (failing tests) → Task 2 (implement) → Task 4 (docs + regenerate).
Task 1 precedes the handler because Task 2 reads the new settings. Tests precede
the implementation per TDD.
