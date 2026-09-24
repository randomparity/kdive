# powerpc PMD migration entry VM_BUG_ON on unmap

## Summary

- **Subsystem**: powerpc book3s64 memory management (`arch/powerpc/mm/book3s64/pgtable.c`)
- **Fix reference**: [bbcbf045d6c7](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=bbcbf045d6c778e82b47a35fc8728387708e9a3d) "powerpc/64s: Fix unmap race with PMD migration entries"
- **Introduced by**: 75358ea359e7 "powerpc/mm/book3s64: Fix MADV_DONTNEED and parallel page fault race" (v5.8-rc1), and a30b48bf1b24 "mm/migrate_device: implement THP migration of zone device pages"
- **Fixed release**: Linux 7.1-rc1
- **Architecture**: ppc64le only (book3s64, radix or hash)
- **Primary symptom**: `kernel BUG at arch/powerpc/mm/book3s64/pgtable.c` (Program Check, vector 700)
- **VM suitability**: Medium. The `test_hmm` path is close to deterministic; the `move_pages` path is a race.
- **KDIVE tools exercised**: `control.watch_for_crash`, `vmcore.fetch`, `postmortem.crash`, `introspect.from_vmcore`, `systems.snapshot`, `systems.restore`, `debug.load_module_symbols`

## Bug description

`pmdp_huge_get_and_clear_full()` has a `VM_BUG_ON` that assumes a present PMD. During THP
migration the PMD is briefly a migration swap entry, which `__pmd_trans_huge_lock()` still
accepts. A parallel `munmap()` then reaches the `VM_BUG_ON`. Device-private THP migration
(`test_hmm`) reaches the same state.

## Suggested starting prompt

> On POWER, a program that migrates transparent huge pages while another thread unmaps them
> crashes the kernel with a BUG in `pmdp_huge_get_and_clear_full()`. Explain the PMD state at
> the crash.

## Reproduction sketch

1. Build the kernel with `CONFIG_DEBUG_VM=y`, `CONFIG_TRANSPARENT_HUGEPAGE=y`, and `CONFIG_TEST_HMM=m`.
2. Either load `test_hmm` and run the `hmm-tests` selftest, or loop `move_pages()` on a THP
   range in one thread and `munmap()` of it in another.
3. Capture the vmcore and read the PMD value at the crash.
4. Snapshot the guest before the loop so each attempt starts from the same state.

## Expected signal on vulnerable kernel

- `kernel BUG` in `pmdp_huge_get_and_clear_full()` from `zap_huge_pmd()`.
- The PMD in the vmcore is a non-present migration entry.

## Fixed-kernel expectation

The unmap handles migration entries and the loop runs without a BUG.

## A/B scoring hints

- Award credit for decoding the PMD value as a migration entry.
- Award extra credit for naming the two racing paths.
- Penalize a fix that removes the `VM_BUG_ON` without handling the entry.

## Caveats

`CONFIG_DEBUG_VM` is required; without it the check is compiled out. This case gives
ppc64le-specific crash evidence on a POWER host.
