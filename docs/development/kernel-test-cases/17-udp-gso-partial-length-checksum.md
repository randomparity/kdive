# UDP GSO_PARTIAL wrong length and checksum on the wire

## Summary

- **Subsystem**: networking, UDP segmentation offload (`net/ipv4/udp_offload.c`)
- **Fix reference**: [78effd896eee](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=78effd896eee11ac9db9bcbb53e7bbcad96073d7) "udp: Fix UDP length on last GSO_PARTIAL segment" and [5f17ae0f595a](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=5f17ae0f595aeb560155ce98edbe44d3eacc7e40) "udp: gso: Fix handling checksum in __udp_gso_segment"
- **Introduced by**: b10b446ce7ad "udp: gso: Use single MSS length in UDP header for GSO_PARTIAL" (v7.0-rc1)
- **Fixed release**: Linux 7.1-rc5
- **Architecture**: generic
- **Primary symptom**: Wrong UDP length or bad UDP checksum in segmented packets
- **VM suitability**: Medium. Needs a sender that uses `UDP_SEGMENT` and software segmentation.
- **KDIVE tools exercised**: `control.capture_traffic`, `debug.set_breakpoint`, `debug.set_watchpoint`, `debug.read_memory`, `introspect.run`

## Bug description

Two defects are in `__udp_gso_segment()`. First, when the payload is an exact multiple of the
MSS, the last segment is itself a GSO skb, and its UDP length is wrong. Second, `uh->len` comes
from the single-MSS length while `uh->check` is adjusted by a different length, so a
software-finished checksum is wrong.

## Suggested starting prompt

> A UDP sender that uses `UDP_SEGMENT` loses datagrams on 7.0 kernels. The receiver counts
> checksum and length errors. Find what is wrong with the segments on the wire.

## Reproduction sketch

1. Set `UDP_SEGMENT` on a socket and send exactly N x MSS bytes.
2. Force software segmentation and checksums (`ethtool -K <dev> tx-udp-segmentation off
   tx-checksumming off`, or a veth or tunnel path).
3. Capture the traffic and read each segment's UDP length and checksum.
4. Compare with a send of N x MSS + 1 bytes and with the fixed kernel.

## Expected signal on vulnerable kernel

- The capture shows bad UDP checksums or a wrong length on the last segment.
- `UdpInErrors` or `UdpInCsumErrors` grow on the receiver.

## Fixed-kernel expectation

All segments carry a correct length and checksum.

## A/B scoring hints

- Award credit for evidence taken from the capture, not only from counters.
- Award extra credit for tying the error to the exact-multiple-of-MSS case.
- Penalize if the agent blames the NIC offload without checking the software path.

## Caveats

This is a v7.0 regression (introduced in v7.0-rc1). Hardware checksum offload hides the
checksum defect, so force the software path.
