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

**Handler shape (decided — do not deviate):** keep `artifacts_get` a module-level
async function. Add one optional keyword parameter
`store_factory: Callable[[], _SearchStore] = object_store_from_env`. This preserves
both existing callers unchanged: the registrar still calls
`_artifacts_get(pool, current_context(), artifact_id=...)`, and the test module
still imports `artifacts_get` directly and injects a fake via
`store_factory=lambda: fake`. Do **not** move it onto `ArtifactReadHandlers`
(that would force a registrar + test import churn for no gain). Extend the
`_SearchStore` Protocol with `presign_get(self, key: str, *, expires_in: int) -> str`
so the injected fake and `ObjectStore` both satisfy it.

**Config reads (decided):** import `kdive.config as config` and the two settings
(`ARTIFACT_INLINE_MAX_BYTES`, `ARTIFACT_DOWNLOAD_TTL_SECONDS`) from
`core_settings`. Read both at the top of `artifacts_get` via `config.require(...)`
(they are defaulted, so `require` never raises). This is the project pattern for
server settings — no construction-time seam, no env injection in the handler test;
the TTL test asserts the **default** (900) is what reaches `presign_get` with no env
set. Pass the int cap and TTL as locals into the enrichment helper.

- Behavior, after the existing `_authorized_redacted_artifact` gate returns a key:
  1. Build the store via the factory. On `CategorizedError`, return the metadata
     envelope (`available`, `refs={"object": key}`) plus
     `data={"content_unavailable": "store_unconfigured"}`. No hard failure.
  2. `head(key)` off-thread (`asyncio.to_thread`). On `CategorizedError` or
     `head is None`, return metadata envelope + `content_unavailable: store_error`.
  3. `presign_get(key, expires_in=<TTL local>)` off-thread → `refs["download_uri"]`.
     On `CategorizedError`, fall to the `content_unavailable: store_error` path
     (no URI, no content).
  4. If `head.size_bytes > ARTIFACT_INLINE_MAX_BYTES`: set
     `data["content_omitted"] = "artifact_too_large"`, `data["size_bytes"]`, and
     return with the URI; do not call `get_artifact`.
  5. Else `get_artifact(key, head.etag)` off-thread; if
     `fetched.sensitivity is not Sensitivity.REDACTED` return a not-found-shaped
     `_config_error(artifact_id)` (the redaction gate, same as `search_text`). On
     `CategorizedError`, `content_unavailable: store_error`. Else set
     `data["content"] = fetched.data.decode("utf-8", errors="replace")`,
     `data["content_truncated"] = "false"`, `data["size_bytes"]`, return with URI.
- Reuse `Sensitivity`, `_config_error`, `asyncio.to_thread`. The `_SearchStore`
  Protocol gains `presign_get(self, key: str, *, expires_in: int) -> str` (see the
  handler-shape note above).

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
fake with `presign_get(self, key, *, expires_in) -> str` (record `expires_in`,
return a stub URL) and call `artifacts_get(pool, ctx, artifact_id=..., store_factory=lambda: fake)`.
Add cases from the spec's Tests section:

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
- `expires_in` passed to `presign_get` equals the default TTL (900) with no env
  set (`fake.expires_in == 900`).

Each test fails first for the expected reason (the current `artifacts_get` returns
no `data`), then passes after Task 2.

**Verify-it-catches-failure:** confirm the ≤cap test fails on the current handler
(no `content` key) before implementing.

## Task 4 — Registrar doc + generated reference

- Update the `artifacts.get` tool docstring/description in
  `src/kdive/mcp/tools/catalog/artifacts/registrar.py` to state it returns the
  redacted content inline (≤cap) or via `refs["download_uri"]`. Keep `read_only()`
  annotation and `maturity: "partial"` (the schema stays flat — ADR-0113).
- The registrar call site is unchanged — `artifacts_get` stays a module function
  (the `store_factory` default is `object_store_from_env`).
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
