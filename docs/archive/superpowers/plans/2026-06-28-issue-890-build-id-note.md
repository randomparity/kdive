# Issue 890 Build-Id Note Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept GNU build-id notes in any bounded SHT_NOTE section during external
`vmlinux` validation.

**Architecture:** Reuse the existing ranged ELF parser. Remove the section-name gate in
`_find_build_id_note()` and let `parse_gnu_build_id()` identify GNU build-id records by
note owner/type.

**Tech Stack:** Python 3.14, `struct`, pytest, existing build-artifact validation helpers.

---

### Task 1: Accept GNU Build-Id Notes In Any SHT_NOTE Section

**Files:**
- Modify: `tests/providers/local_libvirt/test_validate_external_artifacts.py`
- Modify: `tests/providers/test_patch_target_paths.py`
- Modify: `src/kdive/build_artifacts/validation.py`

- [ ] **Step 1: Add a configurable ELF note-section fixture**

Change `_elf_with_build_id` in
`tests/providers/local_libvirt/test_validate_external_artifacts.py`:

```python
def _elf_with_build_id(build_id: bytes, *, note_section_name: bytes = b".note.gnu.build-id") -> bytes:
    """Minimal ELF64-LE blob carrying a GNU build-id SHT_NOTE section."""
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    shstrtab = b"\x00.shstrtab\x00" + note_section_name + b"\x00"
    name_shstrtab = shstrtab.index(b".shstrtab")
    name_note = shstrtab.index(note_section_name)
```

- [ ] **Step 2: Write the failing regression test**

Add this test near the other build-id extraction tests:

```python
def test_extract_build_id_accepts_nonstandard_note_section_name() -> None:
    build_id = bytes.fromhex("0123456789abcdef")
    blob = _elf_with_build_id(build_id, note_section_name=b".notes")
    store = _FakeStore({"v": blob}, {})

    assert extract_build_id_ranged(store, "v", max_size=len(blob)) == build_id.hex()
```

- [ ] **Step 3: Run the regression test and verify it fails**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_validate_external_artifacts.py::test_extract_build_id_accepts_nonstandard_note_section_name -q
```

Expected: fail with `vmlinux carries no .note.gnu.build-id section`.

- [ ] **Step 4: Implement SHT_NOTE scanning**

Change `_find_build_id_note()` in `src/kdive/build_artifacts/validation.py`:

```python
        if sh_type != _SHT_NOTE:
            continue
        notes = _read_section(store, key, sht, e_shentsize, i, max_size=max_size)
        try:
            return parse_gnu_build_id(notes)
        except CategorizedError:
            continue
    raise _build_failure("vmlinux carries no GNU build-id note")
```

- [ ] **Step 5: Update old section-name error expectation**

In `tests/providers/test_patch_target_paths.py`, change the assertion that expects
`"vmlinux carries no .note.gnu.build-id section"` to expect
`"vmlinux carries no GNU build-id note"`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_validate_external_artifacts.py tests/providers/test_patch_target_paths.py -q
```

Expected: both focused test modules pass.

- [ ] **Step 7: Run relevant quality gates**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
