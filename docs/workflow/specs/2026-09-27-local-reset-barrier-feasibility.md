# Local-libvirt RESET barrier feasibility: old process and domain retirement

Issue #2842 investigates one candidate under [ADR-0689](../../adr/0689-prove-operator-power-quiescence.md):
terminate the exact old QEMU process, undefine the domain, and retire its UUID without reuse.
The required result is a pass, fail, or unknown for a recovery witness that excludes a late
RESET accepted by libvirt before client termination. This report covers local-libvirt RESET
only. It changes no provider behavior or deadline.

## Verdict

**Unknown for the combined old-process-exit and actual UUID-retirement candidate.** Source
disproves two weaker observations: libvirt 12.0.0 can stop the old QEMU process and start a new
one as part of the same TDX RESET, and successful `virDomainUndefineFlags` can leave an active
domain in the UUID lookup table. Neither source path shows an accepted RESET mutating after
*both* old-process exit and verified UUID absence. No source proof yet establishes that the
combined barrier is safe across queued dispatch, a held domain reference, and TDX recreation.
No delayed-dispatch live proof ran, so the candidate cannot support a finite provider-call
deadline.

This is an inconclusive result for the approved candidate, not a failure inferred from its
individual parts. A narrower non-TDX candidate would need separate approval. A stronger
process-epoch barrier would likewise need its own scope and proof.

## Source evidence and ordering

1. KDIVE's [power handler](../../../src/kdive/jobs/handlers/control/control.py) resolves a READY
   System to a recorded or derived domain name and invokes the provider while holding its
   System fence. [Local control](../../../src/kdive/providers/local_libvirt/lifecycle/control.py)
   looks up that name and calls `domain.reset(0)`. RESET does not inspect domain XML, launch
   security, UUID, or process identity. The [KDIVE XML renderer][kdive-xml] does not emit TDX
   launch security, but the power entry does not verify the live XML against that renderer.
2. [Libvirt RPC dispatch][rpc-server] can queue an accepted request with a client reference;
   the server worker later dispatches it without a closed-client test. Client exit therefore
   does not prove the RESET was rejected or completed.
3. In [libvirt's QEMU RESET handler][qemu-reset], domain lookup precedes the normal MODIFY job
   and monitor `system_reset`. Its TDX branch instead calls
   `qemuProcessFakeRebootViaRecreate`. That [recreate path][qemu-recreate] stops the old QEMU
   process and then starts QEMU again for the same RESET. Old-process exit can precede a later
   mutation by that accepted request.
4. [Libvirt undefine][qemu-undefine] takes a MODIFY job. For an active domain, it only marks the
   domain transient; removal runs only when inactive. The [remove function][qemu-remove] then
   removes the domain from the lookup list. Undefine success alone is therefore not evidence
   of UUID retirement. A RESET blocked in a domain job can also prevent undefine from finishing;
   source does not establish a finite recovery bound for that case.

The source inspected is libvirt **v12.0.0**, matching the `qemu:///session` libvirt API version
reported by `virsh -c qemu:///session version` on this x86_64 host. That session has no domains.
The [live-testing runbook](../../operating/runbooks/live-testing.md) requires a bootable
`KDIVE_LIVE_VM_ROOTFS` qcow2 for a throwaway domain; the repository's
`resolve_throwaway_contract("qemu:///session")` fixture gate reported `absent` with
`KDIVE_LIVE_VM_ROOTFS unset; point it at a bootable rootfs qcow2`. No deterministic delay
harness for libvirt's server queue, domain lookup, job entry, and monitor or recreate work is
documented in that runbook or present in `tests/live_vm/` and `scripts/live-stack/`. No guest or
delayed-dispatch fixture was used and no live proof ran.

A passing investigation would need those delays on a disposable real libvirt VM, exact process
and UUID identity observations, and a race showing the barrier never acknowledges before every
accepted old RESET effect is excluded. It must also account for the ppc64le target rather than
infer it from this x86_64 host. Until then, source and live evidence leave the combined
candidate unknown.

## Effect on the power-call contract

Local-libvirt RESET retains [ADR-0687](../../adr/0687-fence-power-provider-io.md)'s System
fence and wait through provider completion or cancellation. No finite provider-call deadline,
client-kill-and-unlock path, schema, or admission change follows from this result. Other power
actions and remote-libvirt have no verdict from this investigation. Host or daemon reboot is not
a recovery path under ADR-0689.

[kdive-xml]: ../../../src/kdive/providers/local_libvirt/lifecycle/xml.py
[rpc-server]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/rpc/virnetserver.c#L144-L240
[qemu-reset]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_driver.c#L1963-L2015
[qemu-recreate]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_process.c#L449-L509
[qemu-undefine]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_driver.c#L6579-L6683
[qemu-remove]: https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_domain.c#L6020-L6038
