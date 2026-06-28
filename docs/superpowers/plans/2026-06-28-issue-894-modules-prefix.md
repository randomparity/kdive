# Issue 894 Modules Prefix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local-libvirt install accept combined kernel tar members rooted at
`./lib/modules/...`, matching `runs.complete_build` validation.

**Architecture:** Keep normalization at the repack boundary. `repack_modules_subtree()`
already computes a safe normalized path for matching; write a copied `TarInfo` with that
normalized name into the modules-only archive.

**Tech Stack:** Python 3.14, `tarfile`, pytest, existing local-libvirt lifecycle helpers.

---

### Task 1: Canonicalize Repacked Module Member Names

**Files:**
- Modify: `tests/providers/local_libvirt/test_install.py`
- Modify: `src/kdive/providers/local_libvirt/lifecycle/kernel_bundle.py`

- [ ] **Step 1: Write the failing regression test**

Add this test after `test_repack_modules_subtree_skips_path_traversal_members`:

```python
@pytest.mark.parametrize("prefix", ("./", "/"))
def test_repack_modules_subtree_normalizes_prefixed_members(
    tmp_path: Path, prefix: str
) -> None:
    version = "7.0.0-dirty"
    combined = tmp_path / "kernel.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, f"{prefix}boot/vmlinuz", b"bz")
        _tar_add(tar, f"{prefix}lib/modules/{version}/modules.dep", b"")
        _tar_add(tar, f"{prefix}lib/modules/{version}/kernel/ok.ko", b"mod")
    combined.write_bytes(buf.getvalue())

    out = tmp_path / "modules.tar.gz"
    assert repack_modules_subtree(combined, out)

    with tarfile.open(out, "r:gz") as repacked:
        names = set(repacked.getnames())
    assert names == {
        f"lib/modules/{version}/modules.dep",
        f"lib/modules/{version}/kernel/ok.ko",
    }
    assert _RealGuestKernelWriter._read_release(out, "ov") == version
```

- [ ] **Step 2: Run the regression test and verify it fails**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_install.py::test_repack_modules_subtree_normalizes_prefixed_members -q
```

Expected: fail because the repacked archive still contains prefixed `lib/modules/...`
names.

- [ ] **Step 3: Implement normalized repack names**

Change the write path in
`src/kdive/providers/local_libvirt/lifecycle/kernel_bundle.py`:

```python
                if normalized.startswith(_MODULES_MEMBER_PREFIX):
                    safe_member = member.replace(name=normalized)
                    out.addfile(
                        safe_member,
                        src.extractfile(member) if member.isfile() else None,
                    )
                    found = True
```

- [ ] **Step 4: Run focused install tests**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_install.py -q
```

Expected: local-libvirt install tests pass, with the live VM test skipped unless the host
has live VM configuration.

- [ ] **Step 5: Run relevant quality gates**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
