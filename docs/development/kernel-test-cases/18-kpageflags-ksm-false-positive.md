# /proc/kpageflags reports KPF_KSM for every anonymous page

## Summary

- **Subsystem**: procfs, `fs/proc/page.c`
- **Fix reference**: [81401cebfc15](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=81401cebfc1598306b0a981b5f9ee5b58c1aac52) "fs/proc: fix KPF_KSM reported for all anonymous pages"
- **Introduced by**: dee3d0bef2b0 "proc: rewrite stable_page_flags()" (v6.10-rc1)
- **Fixed release**: in mainline after Linux 7.2-rc2; no release tag yet
- **Architecture**: generic
- **Primary symptom**: `KPF_KSM` (bit 21) is set for anonymous pages with KSM never enabled
- **VM suitability**: Easy. Twenty-line program, deterministic.
- **KDIVE tools exercised**: `debug.set_breakpoint`, `debug.read_registers`, `debug.read_memory`, `debug.disassemble`, `debug.resolve_symbol`, `debug.advance`, `introspect.run`

## Bug description

`FOLIO_MAPPING_KSM` equals `FOLIO_MAPPING_ANON | FOLIO_MAPPING_ANON_KSM`. The check
`mapping & FOLIO_MAPPING_KSM` in `stable_page_flags()` is therefore true for every
anonymous folio.

## Suggested starting prompt

> A memory tool reports that every anonymous page is KSM-merged, but KSM is off. Find out
> whether the tool or the kernel is wrong.

## Reproduction sketch

1. Allocate and touch one anonymous page.
2. Read its PFN from `/proc/self/pagemap`.
3. Read the PFN's entry in `/proc/kpageflags` and check bit 21.
4. Break on `stable_page_flags` and read `folio->mapping` and the mask in the compare.

## Expected signal on vulnerable kernel

- Bit 21 (`KPF_KSM`) is set with `/sys/kernel/mm/ksm/run` at 0.
- The low bits of `folio->mapping` show only the anonymous flag.

## Fixed-kernel expectation

`KPF_KSM` is clear for anonymous pages that KSM did not merge.

## A/B scoring hints

- Award credit for proving the page is not a KSM page from kernel state.
- Award extra credit for finding the mask error in the disassembly or source.
- Penalize if the agent blames the user-space tool.

## Caveats

No crash. Live gdbstub debug is not yet proven on ppc64le guests (#2695); run the `debug.*`
part on an x86_64 host.
