# Jump-cursor `artifacts.get` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a byte-offset jump cursor (`find` + `direction`) to `artifacts.get` and remove the `artifacts.search_text` MCP tool, resolving #939.

**Architecture:** A new pure byte-space matcher (`security/artifacts/artifact_jump.py`) locates a literal term over the whole fetched ≤1 MiB body and returns one direction-anchored window. The `artifacts.get` handler gains `find`/`direction`, sharing one redaction-gated fetch. The `search_text` *tool layer* is deleted; the `search_text()` library matcher stays (boot-evidence depends on it).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. FastMCP tool wrappers over plain async handlers.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-30-issue-939-artifact-get-jump-cursor-design.md`; ADR: `docs/adr/0283-artifact-get-jump-cursor.md`.
- Matching is literal, byte-space (UTF-8-encoded terms, `bytes in bytes`), line boundaries on `\n` only, no Unicode normalization, no regex (ADR-0064 anti-ReDoS).
- Guardrails per commit: `just lint`, `just type` (whole tree), `just test` (the relevant subset locally; full suite before push). CI gates recipes individually, so also `just docs-check`, `just rbac-matrix` (regenerate), `just adr-status-check` already green.
- No ADR-NNNN strings in any agent-facing tool/field description (guard `tests/mcp/core/test_no_adr_leak.py`).
- Ruff line length 100. Absolute imports only. Google-style docstrings on public APIs.
- `_MAX_WINDOWED_FETCH_BYTES = 1 MiB`; `ARTIFACT_GET_WINDOW_MAX_BYTES = 24 KiB`; `KDIVE_ARTIFACT_INLINE_MAX_BYTES` default 64 KiB. Effective window cap = `min(max_bytes, inline_cap, 24 KiB)`.
- Do **not** edit historical specs under `docs/specs/` or `docs/archive/`; they are point-in-time records.

---

### Task 1: Pure byte-space jump matcher

**Files:**
- Create: `src/kdive/security/artifacts/artifact_jump.py`
- Test: `tests/security/artifacts/test_artifact_jump.py`

**Interfaces:**
- Consumes: nothing (pure function over `bytes`).
- Produces:
  - `JumpDirection = Literal["forward", "backward"]`
  - `@dataclass(frozen=True, slots=True) class JumpHit: match_offset: int; match_line: int; window_start: int; content: bytes; next_offset: int | None`
  - `def resolve_anchor(size: int, *, direction: JumpDirection, byte_offset: int) -> int` (public; reused by the no-`find` backward handler path)
  - `def jump_find(body: bytes, *, terms: tuple[str, ...], direction: JumpDirection, byte_offset: int, max_bytes: int) -> JumpHit | None`

- [ ] **Step 1: Write the failing tests** in `tests/security/artifacts/test_artifact_jump.py`:

```python
from kdive.security.artifacts.artifact_jump import JumpHit, jump_find

BODY = b"line one\nBUG: panic here\nline three\ntail BUG: again\nlast\n"
#       0         10                30          42                58


def test_forward_first_hit_from_start():
    hit = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=24 * 1024)
    assert hit is not None
    assert hit.match_offset == BODY.index(b"BUG:")
    assert hit.match_line == 2
    assert b"BUG: panic here" in hit.content
    # next_offset points past the matched line (to the next line start)
    assert hit.next_offset == BODY.index(b"line three")


def test_forward_paging_enumerates_then_exhausts():
    first = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert first is not None and first.next_offset is not None
    second = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=first.next_offset, max_bytes=4096)
    assert second is not None
    assert second.match_offset == BODY.index(b"tail BUG:") + len("tail ")
    # no further matches forward
    assert second.next_offset is not None
    third = jump_find(BODY, terms=("BUG:",), direction="forward", byte_offset=second.next_offset, max_bytes=4096)
    assert third is None


def test_backward_default_offset_starts_from_end():
    # byte_offset 0 in backward == end-of-artifact
    hit = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=0, max_bytes=4096)
    assert hit is not None
    assert hit.match_offset == BODY.index(b"tail BUG:") + len("tail ")  # the LAST match
    assert hit.next_offset is not None and hit.next_offset < hit.match_offset


def test_backward_negative_offset_also_end():
    hit = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=-1, max_bytes=4096)
    assert hit is not None and hit.match_offset == BODY.index(b"tail BUG:") + len("tail ")


def test_backward_paging_walks_up_then_exhausts():
    last = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=0, max_bytes=4096)
    assert last is not None
    prev = jump_find(BODY, terms=("BUG:",), direction="backward", byte_offset=last.next_offset, max_bytes=4096)
    assert prev is not None and prev.match_offset == BODY.index(b"BUG: panic")
    assert prev.next_offset is None  # first line reached


def test_or_terms_jump_to_nearest_forward():
    hit = jump_find(BODY, terms=("absent", "three"), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None and hit.match_offset == BODY.index(b"three")


def test_no_match_returns_none():
    assert jump_find(BODY, terms=("nonexistent",), direction="forward", byte_offset=0, max_bytes=4096) is None
    assert jump_find(BODY, terms=("nonexistent",), direction="backward", byte_offset=0, max_bytes=4096) is None


def test_match_at_offset_zero():
    body = b"BUG: at start\nmore\n"
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None and hit.match_offset == 0 and hit.window_start == 0 and hit.match_line == 1


def test_match_at_eof_no_trailing_newline():
    body = b"first\nlast line BUG:"  # no trailing \n
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None
    assert b"BUG:" in hit.content
    assert hit.next_offset is None  # nothing after the final line


def test_long_line_anchors_window_at_match():
    # a single line longer than the cap, with the term late in the line
    body = b"X" * 30000 + b"BUG:" + b"Y" * 100
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=24 * 1024)
    assert hit is not None
    assert b"BUG:" in hit.content  # term must be in-window despite the long line
    assert hit.window_start == body.index(b"BUG:")


def test_byte_space_no_unicode_oversplit():
    # U+2028 (line separator) must NOT act as a line boundary; \n only
    body = "a b BUG: x\nnext\n".encode("utf-8")
    hit = jump_find(body, terms=("BUG:",), direction="forward", byte_offset=0, max_bytes=4096)
    assert hit is not None and hit.match_line == 1  # whole first physical line
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/security/artifacts/test_artifact_jump.py -q`
Expected: FAIL (`ModuleNotFoundError: kdive.security.artifacts.artifact_jump`).

- [ ] **Step 3: Implement** `src/kdive/security/artifacts/artifact_jump.py`:

```python
"""Byte-space literal jump matcher for ``artifacts.get`` (#939).

Locates a literal ``|``-OR term over the whole fetched artifact body and returns one
direction-anchored window plus a strictly-advancing continuation cursor. Matching is on raw
bytes (UTF-8-encoded terms) with line boundaries on ``\\n`` only, so the byte-offset cursor
``artifacts.get`` already uses stays exact and Unicode line separators do not over-split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

JumpDirection = Literal["forward", "backward"]


@dataclass(frozen=True, slots=True)
class JumpHit:
    """One located match and its returned context window."""

    match_offset: int
    match_line: int
    window_start: int
    content: bytes
    next_offset: int | None


def resolve_anchor(size: int, *, direction: JumpDirection, byte_offset: int) -> int:
    """Resolve the search anchor to the direction's natural edge when unset/degenerate."""
    if direction == "forward":
        return max(byte_offset, 0)
    if byte_offset <= 0:  # omitted/0/negative in backward => end-of-artifact
        return size
    return min(byte_offset, size)


def _first_match_at_or_after(body: bytes, terms_b: tuple[bytes, ...], start: int) -> int | None:
    best: int | None = None
    for term in terms_b:
        i = body.find(term, start)
        if i != -1 and (best is None or i < best):
            best = i
    return best


def _last_match_at_or_before(body: bytes, terms_b: tuple[bytes, ...], end: int) -> int | None:
    best: int | None = None
    for term in terms_b:
        i = body.rfind(term, 0, end + 1)
        if i != -1 and (best is None or i > best):
            best = i
    return best


def _line_bounds(body: bytes, offset: int) -> tuple[int, int]:
    """Return ``(line_start, line_end)`` for the ``\\n``-delimited line containing ``offset``."""
    line_start = body.rfind(b"\n", 0, offset) + 1
    nl_after = body.find(b"\n", offset)
    line_end = nl_after if nl_after != -1 else len(body)
    return line_start, line_end


def jump_find(
    body: bytes,
    *,
    terms: tuple[str, ...],
    direction: JumpDirection,
    byte_offset: int,
    max_bytes: int,
) -> JumpHit | None:
    """Locate the next/previous literal match and return its anchored window, or ``None``."""
    terms_b = tuple(term.encode("utf-8") for term in terms)
    size = len(body)
    anchor = resolve_anchor(size, direction=direction, byte_offset=byte_offset)
    if direction == "forward":
        match = _first_match_at_or_after(body, terms_b, anchor)
    else:
        match = _last_match_at_or_before(body, terms_b, anchor)
    if match is None:
        return None
    line_start, line_end = _line_bounds(body, match)
    match_line = body.count(b"\n", 0, match) + 1
    if direction == "forward":
        window_start = line_start if (match - line_start) < max_bytes else match
        window_end = min(size, window_start + max_bytes)
        next_offset = line_end + 1 if line_end < size else None
        if next_offset is not None and next_offset >= size:
            next_offset = None
    else:
        window_end = line_end
        window_start = max(0, window_end - max_bytes)
        if match < window_start:  # long line pushed the match out of the window
            window_start = match
            window_end = min(size, match + max_bytes)
        next_offset = line_start - 1 if line_start > 0 else None
    return JumpHit(
        match_offset=match,
        match_line=match_line,
        window_start=window_start,
        content=body[window_start:window_end],
        next_offset=next_offset,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/security/artifacts/test_artifact_jump.py -q`
Expected: PASS (all). Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/security/artifacts/artifact_jump.py tests/security/artifacts/test_artifact_jump.py
git commit -m "feat(939): byte-space jump matcher for artifacts.get"
```

---

### Task 2: Wire `find`/`direction` into the `artifacts.get` handler

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/reads.py` (`artifacts_get` and `_artifact_content`)
- Test: `tests/mcp/catalog/test_artifacts_tools.py` (add to the existing `artifacts.get` suite)

**Interfaces:**
- Consumes: `jump_find`, `JumpHit`, `JumpDirection` (Task 1); `parse_literal_terms`, `ArtifactSearchInputError` (existing `artifact_search.py`).
- Produces: `artifacts_get(pool, ctx, *, artifact_id, byte_offset=0, max_bytes=..., find=None, direction="forward", store_factory=...)` returning the existing envelope; with `find`, `data` adds `match_found` (bool) and on a hit `match_offset`, `match_line`, `content`, `next_offset`.

**Approach:** Extract the fetch+redaction-gate+gzip path from `_artifact_content` into a shared loader so plain windowing, no-`find` backward windowing, and `find` matching all reuse one redaction gate (no duplicated gate = no drift risk). The existing `artifacts.get` test suite is the regression guard for the plain path — keep it byte-identical.

- [ ] **Step 1: Write failing handler tests.** Use the concrete harness already in `tests/mcp/catalog/test_artifacts_tools.py`: the `migrated_url: str` fixture, `async with _pool(migrated_url) as pool`, `_, _, red_id = await _seed_system_with_artifacts(pool)` (third element is the redacted id), `_ctx()` for a viewer context, and `_SearchStore(body)` passed as `store_factory=lambda: store` (the fake ignores the key and serves `body`; `_SearchStore(b"", size=N)` makes `head` report `N` bytes). A failure envelope is `resp.status == "error"` with `resp.data["reason"]` (see the existing oversized search test ~line 292). Add:

```python
def test_get_find_forward_returns_match_window(migrated_url: str) -> None:
    body = b"boot ok\nBUG: KASAN slab-out-of-bounds\nCall Trace:\n func+0x1\n"
    store = _SearchStore(body)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store, find="BUG: KASAN"
            )
            assert resp.status == "available"
            assert resp.data["match_found"] is True
            assert resp.data["match_line"] == 2
            assert "BUG: KASAN" in str(resp.data["content"])
            assert resp.data["match_offset"] == body.index(b"BUG: KASAN")

    asyncio.run(_run())


def test_get_find_no_match(migrated_url: str) -> None:
    store = _SearchStore(b"clean boot\nno crash here\n")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store, find="BUG:"
            )
            assert resp.status == "available"
            assert resp.data["match_found"] is False
            assert "content" not in resp.data

    asyncio.run(_run())


def test_get_find_backward_from_end(migrated_url: str) -> None:
    body = b"BUG: first\nmid\nBUG: second\nend\n"
    store = _SearchStore(body)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store,
                find="BUG:", direction="backward",
            )
            assert resp.data["match_offset"] == body.rindex(b"BUG:")

    asyncio.run(_run())


def test_get_find_oversized_rejects(migrated_url: str) -> None:
    store = _SearchStore(b"", size=1024 * 1024 + 1)  # head reports > 1 MiB

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store, find="BUG:"
            )
            assert resp.status == "error"
            assert resp.data["reason"] == "artifact_too_large"

    asyncio.run(_run())


def test_get_find_malformed_rejects(migrated_url: str) -> None:
    store = _SearchStore(b"anything")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id, store_factory=lambda: store, find="a||b"
            )
            assert resp.status == "error"
            assert resp.data["reason"] == "bad_search_input"

    asyncio.run(_run())


def test_get_backward_no_find_is_tail(migrated_url: str) -> None:
    body = b"".join(b"line %d\n" % i for i in range(10000))  # > one window

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            resp = await artifacts_get(
                pool, _ctx(), artifact_id=red_id,
                store_factory=lambda: _SearchStore(body), direction="backward",
            )
            assert str(resp.data["content"]).endswith("line 9999\n")
            assert int(resp.data["next_offset"]) > 0

    asyncio.run(_run())
```

Coverage note: `find` reuses `_authorized_redacted_artifact` + the shared loader, so viewer-enforcement, SENSITIVE/non-redacted, and quarantine gates are already exercised by the existing `artifacts.get` tests; Task 4 maps the deleted search-tool assertions onto these.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -k "find or tail" -q`
Expected: FAIL (`artifacts_get() got an unexpected keyword argument 'find'`).

- [ ] **Step 3: Refactor `_artifact_content` and add the `find`/backward paths** in `reads.py`.

Extract a loader returning the plaintext body or a degraded/`drift` state (preserving every existing branch: `store_unconfigured`, `store_error`, head/fetched non-REDACTED `drift`, `artifact_too_large` omit, gzip `decode_error`). Then:

```python
async def artifacts_get(
    pool, ctx, *, artifact_id, byte_offset=0,
    max_bytes=ARTIFACT_GET_WINDOW_DEFAULT_BYTES,
    find: str | None = None,
    direction: JumpDirection = "forward",
    store_factory=object_store_from_env,
) -> ToolResponse:
    authorized = await _authorized_redacted_artifact(pool, ctx, artifact_id=artifact_id)
    if isinstance(authorized, ToolResponse):
        return authorized
    if find is not None:
        try:
            terms = parse_literal_terms(find)
        except ArtifactSearchInputError:
            return _config_error(artifact_id, data={"reason": "bad_search_input"})
    refs: dict[str, str] = {"object": authorized.key}
    loaded = await _load_redacted_plaintext(authorized.key, store_factory, refs)
    if loaded.drift:
        return _config_error(artifact_id)
    if find is not None:
        data = _find_response_data(loaded, terms=terms, direction=direction,
                                   byte_offset=byte_offset, max_bytes=max_bytes,
                                   artifact_id=artifact_id)
        if isinstance(data, ToolResponse):
            return data
    else:
        data = _window_response_data(loaded, byte_offset=byte_offset,
                                     max_bytes=max_bytes, direction=direction)
    return ToolResponse.success(artifact_id, "available",
                                suggested_next_actions=["artifacts.get"], refs=refs, data=data)
```

Helper rules:
- `_window_response_data`: when `loaded.body is None` return `loaded.degraded`. Forward = today's slice `body[byte_offset:byte_offset+effective_max]` with the existing `content_truncated`/`next_offset`/`size_bytes` keys, **byte-identical to current behavior** (the existing tests guard this). Backward (no find): resolve the window end with the shared `resolve_anchor(size, direction="backward", byte_offset=byte_offset)` from `artifact_jump` (so omitted/`0`/negative `byte_offset` = end-of-artifact, identical to the find path — never re-derive this inline), then `window_start = max(0, end - effective_max)`, `content = body[window_start:end]`, `next_offset = window_start` when `> 0` else omitted.
- `_find_response_data`: when `loaded.body is None`, if the degrade is `artifact_too_large` return `_config_error(artifact_id, data={"reason": "artifact_too_large", "size_bytes": loaded.size_bytes})`; otherwise return `{**loaded.degraded, "match_found": False}` (store outage — degrade, don't lie). When body present, call `jump_find`; `None` → `{"match_found": False, "size_bytes": loaded.size_bytes}`; a `JumpHit` → `{"match_found": True, "size_bytes": loaded.size_bytes, "match_offset": hit.match_offset, "match_line": hit.match_line, "content": hit.content.decode("utf-8", errors="replace")}` plus `"next_offset": hit.next_offset` when not `None`.
- `effective_max = min(max(max_bytes, 1), inline_cap, ARTIFACT_GET_WINDOW_MAX_BYTES)` (unchanged formula).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q`
Expected: PASS — both the new `find`/tail tests and **all pre-existing `artifacts.get` tests** (regression). Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts/reads.py tests/mcp/catalog/test_artifacts_tools.py
git commit -m "feat(939): jump-cursor find/direction on artifacts.get handler"
```

---

### Task 3: Add `find`/`direction` to the `artifacts.get` registrar wrapper

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py` (`_register_artifacts_get`)
- Modify (regenerate): committed tool reference via `just docs`
- Test: `tests/mcp/catalog/test_artifacts_tools.py` (schema advertises the params)

**Interfaces:**
- Consumes: `artifacts_get` (Task 2) and `JumpDirection`.
- Produces: the `artifacts.get` tool schema exposes optional `find: str` and `direction: enum[forward,backward]` with the literal/`|`-OR/byte-space/no-normalization/backward-default contract in the `Field` text.

- [ ] **Step 1: Write the failing schema test**

```python
def test_get_schema_advertises_find_and_direction() -> None:
    props = _tool_param_schema("artifacts.get")  # existing helper: returns properties dict
    assert "find" in props and "direction" in props
    assert "forward" in str(props["direction"]) and "backward" in str(props["direction"])
    assert "no regex" in str(props["find"]["description"]).lower()
```

- [ ] **Step 2: Run to verify failure** — `... -k advertises_find -q` → FAIL (no `find` property).

- [ ] **Step 3: Add the params** to the `artifacts_get` wrapper in `registrar.py` (pass through to the handler):

```python
find: Annotated[
    str | None,
    Field(
        description=(
            "Jump to the next/previous literal match instead of a plain byte window. "
            "'|' separates terms (e.g. 'BUG: KASAN|Call Trace'); the nearest term in "
            "'direction' is returned with data.match_offset/match_line and data.next_offset "
            "to continue. Per-line literal substring, case-sensitive, no regex and no "
            "Unicode normalization (match the artifact's exact bytes; kernel signatures are "
            "ASCII). Omit for a plain window."
        )
    ),
] = None,
direction: Annotated[
    JumpDirection,
    Field(
        description=(
            "Cursor direction for paging and for 'find'. 'forward' starts at byte_offset "
            "(default 0). 'backward' starts at end-of-artifact when byte_offset is omitted "
            "(read the tail and page up); a positive byte_offset bounds it."
        )
    ),
] = "forward",
```

Regenerate and review the committed tool reference: `just docs`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q && just docs-check && just lint && just type && uv run python -m pytest tests/mcp/core/test_no_adr_leak.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts/registrar.py docs/ tests/mcp/catalog/test_artifacts_tools.py
git commit -m "feat(939): advertise find/direction on the artifacts.get tool"
```

---

### Task 4: Remove the `artifacts.search_text` tool

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py` (delete `_register_artifacts_search_text` + its call in `register`)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/reads.py` (delete `ArtifactSearchRequest`, `ArtifactReadHandlers`, `artifacts_search_text`, `_artifacts_search_text`; drop now-unused imports `BEFORE_LINES_RANGE`/`AFTER_LINES_RANGE`/`MAX_MATCHES_RANGE`/`search_text` if no longer referenced — keep `parse_literal_terms`, `ArtifactSearchInputError`)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py` (`_CONSOLE_ACCESS_HINT` + its comment/docstrings)
- Modify (regenerate): `just docs`, `just rbac-matrix`
- Test: delete the `artifacts.search_text` tool tests in `tests/mcp/catalog/test_artifacts_tools.py`; update `_CONSOLE_ACCESS_EXPECTED` in `tests/mcp/lifecycle/test_runs_tools.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `artifacts.search_text` no longer registered; `_CONSOLE_ACCESS_HINT = {"ref": "console", "search": "artifacts.get", "full_text": "artifacts.get"}`.

- [ ] **Step 1: Update the failing-first assertions**

In `tests/mcp/lifecycle/test_runs_tools.py` set `_CONSOLE_ACCESS_EXPECTED["search"] = "artifacts.get"`. Add a guard that `artifacts.search_text` is no longer a registered tool:

```python
def test_search_text_tool_is_removed() -> None:
    # _tool_param_schema builds a DB-free app via build_app + app.get_tool(name)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    assert asyncio.run(app.get_tool("artifacts.search_text")) is None
    assert asyncio.run(app.get_tool("artifacts.get")) is not None
```

Note: `app.get_tool` raises `NotFoundError` rather than returning `None` in some FastMCP
versions; if so, assert with `pytest.raises(NotFoundError)`. Confirm against the real return
by reading how `_tool_param_schema` (which asserts `tool is not None`) behaves for a present
tool, and mirror it.

- [ ] **Step 2: Run to verify failure** — the new `removed` test fails (tool still present); the `_CONSOLE_ACCESS_EXPECTED` test fails.

- [ ] **Step 3: Delete the tool layer.** Remove `_register_artifacts_search_text` and its call; delete `ArtifactSearchRequest`, `ArtifactReadHandlers`, `artifacts_search_text`, `_artifacts_search_text` from `reads.py`; update `_CONSOLE_ACCESS_HINT` (set `"search": "artifacts.get"`) and its comment (search is now `artifacts.get` with `find`). Confirm `security/artifacts/artifact_search.py` and `jobs/handlers/runs/boot_evidence.py` are **untouched**. Regenerate: `just docs && just rbac-matrix`.

  **Before deleting the `artifacts.search_text` tool tests, map each behavior to a survivor** (do not delete blind): the search tool's `requires_viewer`, `sensitive→not-found`/`non-redacted→config-error`, quarantine-exclusion, oversized, malformed-pattern, and store-error tests all assert gates that `artifacts.get` already enforces through the shared `_authorized_redacted_artifact` + loader. For any gate whose **only** assertion lived on the search tool (in particular the quarantine-exclusion case via `_seed_quarantined_artifact`, and the SENSITIVE/non-redacted gate), add the equivalent assertion as a `find=`-mode `artifacts_get` test before removing the search-tool test. Then delete the search-tool tests (`test_artifacts_search_text_*`, `_search_text_param_schema`, `_panic_search`/`_lookup_search_with_context`/`_invalid_pattern_search`, and `_artifact_read_handlers` if unused).

- [ ] **Step 4: Run the full guardrail set**

Run: `just lint && just type && just docs-check && just rbac-matrix-check && just resources-docs-check && uv run python -m pytest tests/mcp -q && uv run python -m pytest tests/security/artifacts tests/jobs -q && uv run python -m pytest tests/mcp/core/test_no_adr_leak.py tests/mcp/core/test_console_surface_docs.py -q`
Expected: PASS. (`resources-docs-check` is included as a belt-and-suspenders gate; no served `_content/*.md` snapshot references `artifacts.search_text`, so it should already be in sync.) Verify `rg -n "artifacts.search_text" src/` returns nothing (historical docs under `docs/specs/` and `docs/archive/` are intentionally left as point-in-time records).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(939)!: remove artifacts.search_text in favor of artifacts.get find"
```

---

## Self-review notes

- **Spec coverage:** find/direction (T1/T2/T3); byte-space matching + long-line + offset-0 + EOF (T1); oversized-find rejection + bad_search_input (T2); backward tail no-find (T2); schema advertisement + Field contract (T3); tool removal + console_access + preserved matcher (T4). Cost/rollback are non-code.
- **Type consistency:** `jump_find`/`JumpHit`/`JumpDirection` names match across T1→T2→T3. `find: str | None`, `direction: JumpDirection` identical in handler and wrapper.
- **Regression guard:** the existing `artifacts.get` suite must stay green through T2's refactor; the existing `search_text()` matcher tests (boot-evidence) stay untouched through T4.
