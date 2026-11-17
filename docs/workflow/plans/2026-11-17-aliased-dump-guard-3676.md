# Aliased dump guard Implementation Plan

**Goal:** a guard test fails on a new unaliased pydantic dump under `src/` or `deploy/`.
**Architecture:** one AST scanner plus a reviewed per-file allowlist in a single guard test.
Spec: `docs/workflow/specs/2026-11-17-aliased-dump-guard-3676-design.md`.
**Tech stack:** Python `ast`, pytest.

## Global Constraints

- No `src/` change; no wire shape, alias or validation change (charter exclusions).
- Do not touch `providers/system_authority/protocol.py`,
  `providers/external_boot_authority/protocol.py` or `remote_module_documents.py` (sibling #3675).
- Guardrails: `just lint`, `just type`, `just test-verbose tests/guards/test_unaliased_dumps_are_reviewed.py`.
- Expected implementation size: 150–230 changed lines (M) — one test file: scanner ~35, tests ~40,
  allowlist ~45 entries at one line each plus reasons.

File map: `tests/guards/test_unaliased_dumps_are_reviewed.py` (new). Owner stays the guards tier;
no caller migration, no obsolete path.

## Task 1: scanner, allowlist, tree test

**Interfaces:** `collect_unaliased_dumps(source: str) -> list[int]` returns the line numbers of
calls named in the spec that carry neither a `by_alias=` keyword nor a `**` argument; `_ALLOWED: dict[str, tuple[int,
str]]`; the tree test uses `Path(__file__).parents[2]` as the repo root. Nothing is consumed from
other tasks.

**Verification:**
- Mode: focused-test. Contract: scanner flags/ignores. Tests `test_scanner_*`; red by
  faulting the scanner (drop the `by_alias` check) and seeing the ignore cases fail. Green:
  `just test-verbose tests/guards/test_unaliased_dumps_are_reviewed.py`.
- Mode: focused-test. Contract: the tree matches the allowlist. Test
  `test_every_unaliased_dump_is_reviewed`; red when a bare `.model_dump()` is added to
  `src/kdive/db/repositories.py` (count) or to an unlisted file; green command as above.

**Steps:**
1. Write the test file with the scanner, scanner cases and an empty `_ALLOWED`; run the tree
   test and confirm it fails listing every file of the 43 (73 sites).
2. Fill `_ALLOWED` with one entry per listed file using reason constants `_NO_ALIAS` or
   `_PYTHON_NAMES`; the four tracer-confirmed sites sit in
   `providers/local_libvirt/external_boot_authority.py`,
   `providers/remote_libvirt/external_boot_authority.py` (both mix several dumps; their reason
   names which are Python-name dumps).
3. Run the green command, then `just lint` and `just type`; commit.
4. Controlled fault (after commit): add `x.model_dump()` to an unlisted `src/` file, observe
   red, `git checkout -- <file>`; repeat for a second dump in an allowlisted file.

Rollback: delete the one file.
