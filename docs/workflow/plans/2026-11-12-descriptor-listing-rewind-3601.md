# Descriptor listing rewind (#3601) — implementation plan

Goal: every decision-bearing `os.listdir(<descriptor>)` under `src/kdive` lists from offset 0,
so btrfs returns entries created after the descriptor's last rewind.

Architecture: one helper, `kdive.providers.shared.directory_listing.list_directory_descriptor`,
seeks the descriptor to 0 and lists it. Thirteen call sites switch to it, one test helper does
too, and an AST guard keeps bare descriptor listings out of `src/kdive`.
Spec: `docs/workflow/specs/2026-11-12-descriptor-listing-rewind-3601-design.md`.

Tech stack: Python 3.14, stdlib `os` and `ast`, pytest.

Expected implementation size: 150–210 changed lines (M) — helper ~20, its test ~35, guard ~85,
13 one-line site edits plus 9 import lines, one test-helper edit.

## Global Constraints

- No new dependency; stdlib only.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict. Every new `src/kdive` module
  needs a test that imports it by dotted path (`tests/guards/test_module_has_direct_test.py`).
- Prose: plain and factual; never "critical", "robust", "comprehensive", "elegant".
- Do not edit `system_authority/journal.py` `_check_evidence_bounds` or
  `jobs/handlers/artifacts/rootfs_reclaim.py` (operator-approved exclusions).
- Guardrails: `just lint`, `just type`, `just test-changed`, `prek run`.

## File map

| File | Change |
|---|---|
| `src/kdive/providers/shared/directory_listing.py` | create: owns rewind-then-list |
| `tests/providers/shared/test_directory_listing.py` | create: helper behavior |
| `tests/guards/test_directory_listing_rewinds.py` | create: AST guard |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session.py` | 1 site |
| `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py` | 1 site |
| `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py` | 5 sites |
| `src/kdive/providers/remote_libvirt/external_boot_authority.py` | 3 sites |
| `src/kdive/providers/remote_libvirt/system_authority.py` | 1 site (`_scan`) |
| `src/kdive/providers/external_boot_authority/host.py` | 1 site |
| `src/kdive/providers/system_authority/journal.py` | 1 site (`system-operations` listing) |
| `src/kdive/jobs/capture_operations/storage/spool.py` | 1 site |
| `tests/providers/remote_libvirt/test_external_boot_authority.py` | `_store_names` |

## Task 1: the helper

Files: create `src/kdive/providers/shared/directory_listing.py`,
`tests/providers/shared/test_directory_listing.py`.

Interfaces: provides `list_directory_descriptor(descriptor: int) -> list[str]`, consumed by
Tasks 2 and 3.

Verification:
- Contract "the helper lists every entry whatever the descriptor's offset" — `Mode:
  focused-test`. Test `test_lists_every_entry_after_the_shared_offset_moved`. Red: with the
  `os.lseek` line removed the assertion sees `[]`. Green:
  `uv run python -m pytest tests/providers/shared/test_directory_listing.py -q` → `1 passed`.

Steps:
1. Write the test:

   ```python
   """Descriptor listings start from offset 0 (#3601)."""

   from __future__ import annotations

   import os
   from pathlib import Path

   from kdive.providers.shared.directory_listing import list_directory_descriptor


   def test_lists_every_entry_after_the_shared_offset_moved(tmp_path: Path) -> None:
       for name in ("a", "b", "c"):
           (tmp_path / name).touch()
       descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
       try:
           # A started scandir reads the whole small directory through a duplicate that shares
           # the offset, leaving it at the end: a bare os.listdir(descriptor) now returns [].
           with os.scandir(descriptor) as entries:
               next(entries)
               assert sorted(list_directory_descriptor(descriptor)) == ["a", "b", "c"]
       finally:
           os.close(descriptor)
   ```
2. Write the module:

   ```python
   """List a directory through a descriptor from its first entry (#3601).

   ``os.listdir(fd)`` reads from the descriptor's shared offset and rewinds only after reading.
   btrfs fixes a directory file's visible index range at its last rewind, so a second listing of
   the same descriptor misses entries created in between; ext4 and tmpfs return them. Seeking to
   0 immediately before the read returns every entry present when the listing starts.
   """

   from __future__ import annotations

   import os


   def list_directory_descriptor(descriptor: int) -> list[str]:
       """Names in the directory open at ``descriptor``, read from offset 0."""
       os.lseek(descriptor, 0, os.SEEK_SET)
       return os.listdir(descriptor)
   ```
3. Run the green command; expect `1 passed`. Comment out the `os.lseek` line, rerun, expect a
   failure showing `[] == ['a', 'b', 'c']`; restore it.
4. Commit: `fix(providers): add a rewinding directory-descriptor listing helper`.

## Task 2: route the sites

Files: the nine source files and `_store_names` in the file map.

Interfaces: consumes `list_directory_descriptor` from Task 1.

Verification:
- Contract "decision-bearing descriptor listings see entries created after the last rewind" —
  `Mode: focused-test`. Red (recorded on `main` aea536906): with `TMPDIR=/home/dave/t` on
  btrfs, `uv run python -m pytest -q tests/providers/local_libvirt/lifecycle/boot/test_session.py
  tests/providers/local_libvirt/lifecycle/boot/test_materializer.py
  tests/providers/remote_libvirt/test_external_boot_authority.py` → `22 failed`. Green: the same
  command → no failures (the 5 `guestfs` import errors in `test_live_guest_regular_bounded_read`
  are an environment gap, unchanged); and the same command without `TMPDIR` → no failures.
- `_store_names` — `Mode: task-test-not-applicable`: test support, observed only through the
  btrfs run above.

Steps:
1. In each file add `from kdive.providers.shared.directory_listing import
   list_directory_descriptor` in the sorted `kdive` import block (`just format` orders it).
2. Replace, keeping each surrounding expression unchanged:
   - `session.py`: `entries = os.listdir(artifact_fd)` → `list_directory_descriptor(artifact_fd)`.
   - `session_mechanisms.py`: `set(os.listdir(projection_fd))` →
     `set(list_directory_descriptor(projection_fd))`.
   - `external_boot.py`: the five `os.listdir(directory_fd)` calls →
     `list_directory_descriptor(directory_fd)`.
   - `external_boot_authority.py`: the three `os.listdir(self._root_fd)` calls →
     `list_directory_descriptor(self._root_fd)`.
   - `system_authority.py` `_scan`: `os.listdir(descriptor)` →
     `list_directory_descriptor(descriptor)`.
   - `host.py`: `os.listdir(root_fd)` → `list_directory_descriptor(root_fd)`.
   - `journal.py` (the `system-operations` block, not `_check_evidence_bounds`):
     `os.listdir(directory_fd)` → `list_directory_descriptor(directory_fd)`.
   - `spool.py` `_remove_spool_entries`: `os.listdir(directory_fd)` →
     `list_directory_descriptor(directory_fd)`.
   - test `_store_names`: `set(os.listdir(store._root_fd))` →
     `set(list_directory_descriptor(store._root_fd))`, adding the import.
3. Run `rg -n "os\.(listdir|scandir)\(" src/`; expect only `directory_listing.py`,
   `journal.py` `_check_evidence_bounds` and `rootfs_reclaim.py`.
4. Run both green commands; then `just lint`, `just type`.
5. Commit: `fix(providers): list directory descriptors from offset 0 on btrfs`.

## Task 3: the guard

Files: create `tests/guards/test_directory_listing_rewinds.py`.

Interfaces: names `src/kdive/providers/shared/directory_listing.py` as the only permitted
listing site besides the allowlist.

Verification:
- Contract "a bare descriptor listing under `src/kdive` fails the suite" — `Mode:
  focused-test`. Red: add `os.listdir(0)` inside any function in `spool.py`, run the guard,
  expect `test_no_bare_directory_listings` to fail naming it; revert. Also delete the
  `rootfs_reclaim.py` allowlist call's match by renaming the allowlist function, expect
  `test_allowlist_entries_still_match` to fail; revert. Green:
  `uv run python -m pytest tests/guards/test_directory_listing_rewinds.py -q` → `2 passed`.

Steps:
1. Write the guard:

   ```python
   """Guard: directory listings in src/kdive go through the rewinding helper (#3601).

   ``os.listdir(fd)`` on a reused descriptor misses entries on btrfs, so every listing goes
   through ``list_directory_descriptor``. The allowlist names the two listings excluded from
   #3601 by operator approval: a descriptor opened immediately before listing, and a path scan.
   """

   from __future__ import annotations

   import ast
   from pathlib import Path

   _SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"
   _HELPER = Path("providers/shared/directory_listing.py")
   _LISTERS = frozenset({"listdir", "scandir"})
   _ALLOWLIST = frozenset(
       {
           (Path("providers/system_authority/journal.py"), "_check_evidence_bounds"),
           (Path("jobs/handlers/artifacts/rootfs_reclaim.py"), "_live_writer_holds_a_partial"),
       }
   )


   def _os_lister_names(tree: ast.Module) -> set[str]:
       return {
           alias.asname or alias.name
           for node in ast.walk(tree)
           if isinstance(node, ast.ImportFrom) and node.module == "os"
           for alias in node.names
           if alias.name in _LISTERS
       }


   def _is_lister(call: ast.Call, imported: set[str]) -> bool:
       func = call.func
       if isinstance(func, ast.Attribute):
           return func.attr in _LISTERS and isinstance(func.value, ast.Name) and func.value.id == "os"
       return isinstance(func, ast.Name) and func.id in imported


   def _listing_sites() -> set[tuple[Path, str, int]]:
       sites: set[tuple[Path, str, int]] = set()
       for path in sorted(_SRC.rglob("*.py")):
           relative = path.relative_to(_SRC)
           if relative == _HELPER:
               continue
           tree = ast.parse(path.read_text(encoding="utf-8"))
           imported = _os_lister_names(tree)
           calls = {
               id(node): node
               for node in ast.walk(tree)
               if isinstance(node, ast.Call) and _is_lister(node, imported)
           }
           enclosed: set[int] = set()
           for function in ast.walk(tree):
               if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                   for node in ast.walk(function):
                       if id(node) in calls:
                           enclosed.add(id(node))
                           sites.add((relative, function.name, node.lineno))
           sites.update((relative, "<module>", calls[key].lineno) for key in calls.keys() - enclosed)
       return sites


   def test_no_bare_directory_listings() -> None:
       offenders = sorted(
           f"{path}:{line} in {function}"
           for path, function, line in _listing_sites()
           if (path, function) not in _ALLOWLIST
       )
       assert not offenders, (
           "list directories with kdive.providers.shared.directory_listing."
           f"list_directory_descriptor, not os.listdir/os.scandir: {offenders}"
       )


   def test_allowlist_entries_still_match() -> None:
       found = {(path, function) for path, function, _line in _listing_sites()}
       assert _ALLOWLIST <= found, f"stale allowlist entries: {sorted(_ALLOWLIST - found)}"
   ```

   A call nested in an inner function is recorded under each enclosing function name; the
   allowlist check uses the pair, so an allowlisted outer function cannot hide a call in a
   different function.
2. Run the green command; expect `2 passed`. Run both red arms, observe each failure, revert.
3. Run `just lint`, `just type`, `just test-changed`.
4. Commit: `test(guards): forbid bare directory-descriptor listings in src`.
