# virtio_net bad checksums for tunneled non-GSO packets

## Summary

- **Subsystem**: networking, `drivers/net/virtio_net.c`
- **Fix reference**: [86c51f0f2313](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=86c51f0f23136ea5ef5541f607287e07150cd23f) "virtio_net: do not allow tunnel csum offload for non GSO packets"
- **Introduced by**: 56a06bd40fab "virtio_net: enable gso over UDP tunnel support." (v6.17-rc1)
- **Fixed release**: Linux 7.2-rc1
- **Architecture**: generic (virtio guests)
- **Primary symptom**: Tunneled packets leave with bad inner and outer transport checksums
- **VM suitability**: Medium. Needs a VXLAN or GENEVE tunnel in the guest and a capture point.
- **KDIVE tools exercised**: `control.capture_traffic`, `debug.load_module_symbols`, `debug.list_modules`, `debug.set_breakpoint`, `debug.read_memory`, `introspect.run`

## Bug description

virtio_net offers checksum offload for UDP-tunneled TCP packets that are not GSO. These
packets reach the host as `CHECKSUM_PARTIAL` with `encapsulation` clear, which the virtio
specification does not describe. Unless the host NIC computes the checksum from
`csum_start`/`csum_offset`, both the inner and outer transport checksums are wrong.

## Suggested starting prompt

> A VXLAN tunnel inside a virtio guest carries no TCP traffic, although small pings through the
> tunnel work. Find what is wrong with the packets that leave the guest.

## Reproduction sketch

1. In the guest, create a VXLAN or GENEVE tunnel over the virtio interface.
2. Send small TCP traffic (no GSO) through the tunnel.
3. Capture the traffic and check the inner and outer checksums.
4. Inspect `skb` fields (`csum_start`, `csum_offset`, `encapsulation`) on the transmit path.

## Expected signal on vulnerable kernel

- The capture shows bad checksums at both layers for non-GSO tunneled TCP packets.

## Fixed-kernel expectation

The driver finishes the checksum in software for these packets and the capture is clean.

## A/B scoring hints

- Award credit for showing both bad checksums in the capture.
- Award extra credit for tying the error to the non-GSO case only.
- Penalize if the agent blames the tunnel driver.

## Caveats

The visible result depends on the host NIC and its offloads. Capture on the guest side of the
virtio interface to see what the guest hands to the host.
