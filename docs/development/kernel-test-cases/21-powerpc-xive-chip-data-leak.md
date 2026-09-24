# powerpc XIVE irq data leak on MSI-X teardown

## Summary

- **Subsystem**: powerpc interrupt controller (`arch/powerpc/sysdev/xive/common.c`)
- **Fix reference**: [6771c54728c2](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=6771c54728c278bf1e4bfdab4fddbbb186e33498) "powerpc/xive: fix kmemleak caused by incorrect chip_data lookup"
- **Introduced by**: cc0cc23babc9 "powerpc/xive: Untangle xive from child interrupt controller drivers" (v6.18-rc1)
- **Fixed release**: Linux 7.1-rc1
- **Architecture**: ppc64le only (pseries or powernv with XIVE)
- **Primary symptom**: One 64-byte `xive_irq_data` object leaks per freed MSI-X vector
- **VM suitability**: Easy. Bind and unbind loop, deterministic.
- **KDIVE tools exercised**: `introspect.script`, `introspect.run`, `systems.snapshot`, `systems.restore`, `runs.bind`, `runs.set`

## Bug description

`xive_irq_alloc_data()` stores its data in the XIVE (parent) domain's `irq_data->chip_data`.
`xive_irq_free_data()` reads it back with `irq_get_chip_data()`, which resolves through the
child domain and gets a different pointer, so the allocation is never freed.

## Suggested starting prompt

> On a POWER guest, kmemleak reports unreferenced 64-byte objects after each unbind of a PCI
> device, with `xive_irq_alloc_data` in the trace. Find why the free path misses them.

## Reproduction sketch

1. Boot with `kmemleak=on` and `CONFIG_DEBUG_KMEMLEAK=y`.
2. Unbind and bind a virtio-pci device in a loop through sysfs.
3. Run `echo scan > /sys/kernel/debug/kmemleak` and read the report.
4. Walk the irq domains and compare `chip_data` in the parent and child `irq_data`.

## Expected signal on vulnerable kernel

- kmemleak lists 64-byte objects from `xive_irq_alloc_data()`.
- The count grows by the number of MSI-X vectors per cycle.

## Fixed-kernel expectation

No leak after the loop.

## A/B scoring hints

- Award credit for counting leaked objects per cycle.
- Award extra credit for showing the parent and child `chip_data` pointers differ.
- Penalize a fix that frees the data in the child domain's teardown.

## Caveats

Runs in a pseries KVM guest on a POWER host. Check `images.describe` for
`capability_signals.live_drgn` before planning the `introspect.*` steps.
