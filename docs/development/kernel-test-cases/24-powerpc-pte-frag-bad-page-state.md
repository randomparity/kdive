# powerpc PTE fragment bad page state at exit

## Summary

- **Subsystem**: powerpc page-table fragments (`arch/powerpc/mm/pgtable-frag.c`)
- **Fix reference**: [fda4d71651f7](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=fda4d71651f71c44b35829d13f3c8bf920032f77) "powerpc/pgtable-frag: Fix bad page state in pte_frag_destroy"
- **Introduced by**: 32cc0b7c9d50 "powerpc: add pte_free_defer() for pgtables sharing page" (v6.6-rc1)
- **Fixed release**: Linux 7.1-rc1
- **Architecture**: ppc64le only; most likely with hash MMU and 64K pages
- **Primary symptom**: `BUG: Bad page state` with the `active` flag set, from `pte_frag_destroy()`
- **VM suitability**: Medium. Loop of `MADV_COLLAPSE` on shmem and exit.
- **KDIVE tools exercised**: `control.watch_for_crash`, `postmortem.crash`, `vmcore.fetch`, `introspect.run`, `debug.set_breakpoint`

## Bug description

`pte_free_defer()` sets the fragment folio's `active` flag. When a process exits with that
folio still cached in `mm->context`, `pte_frag_destroy()` frees it without clearing the flag,
and the page allocator reports a bad page state.

## Suggested starting prompt

> On POWER, a program that collapses shmem ranges into huge pages and then exits triggers
> "BUG: Bad page state ... flags: active" on exit. Find which path frees the page with the flag set.

## Reproduction sketch

1. Map a shmem or tmpfs region and fault in its PTEs.
2. Call `madvise(MADV_COLLAPSE)` on the region, then exit. Loop.
3. Boot with `panic_on_warn=1` to get a vmcore at the first report.
4. Read `pt_frag_refcount` and the folio flags in the crashed kernel.

## Expected signal on vulnerable kernel

- `BUG: Bad page state` with `PAGE_FLAGS_CHECK_AT_FREE flag(s) set` from `pte_frag_destroy()`.

## Fixed-kernel expectation

The loop runs without bad-page reports.

## A/B scoring hints

- Award credit for connecting the `active` flag to `pte_free_defer()`.
- Award extra credit for explaining the cached-fragment exit path.
- Penalize a fix that clears the flag at allocation.

## Caveats

The fix commit describes the hash MMU with 64K pages. Record the guest's MMU mode (the `MMU`
line in `/proc/cpuinfo`) with each attempt.
