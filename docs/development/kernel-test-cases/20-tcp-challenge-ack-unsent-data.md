# TCP sends no challenge ACK for SEG.ACK above SND.NXT

## Summary

- **Subsystem**: networking, TCP input (`net/ipv4/tcp_input.c`)
- **Fix reference**: [42726ec644cb](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=42726ec644cbdde0035c3e0417fee8ed9547e120) "tcp: send a challenge ACK on SEG.ACK > SND.NXT"
- **Introduced by**: 354e4aa391ed "tcp: RFC 5961 5.2 Blind Data Injection Attack Mitigation" (v3.8-rc1)
- **Fixed release**: Linux 7.1-rc1
- **Architecture**: generic
- **Primary symptom**: No ACK on the wire where RFC 5961 and RFC 9293 require one
- **VM suitability**: Easy with packetdrill; the in-tree script drives this path.
- **KDIVE tools exercised**: `control.capture_traffic`, `debug.set_breakpoint`, `introspect.run`, `runs.bind`, `runs.set`

## Bug description

For a segment whose ACK value acknowledges data never sent, Linux drops the segment with
`SKB_DROP_REASON_TCP_ACK_UNSENT_DATA` and sends nothing. The RFCs require an ACK (a challenge
ACK, rate-limited). The lower edge of the window already sends one; the upper edge does not.

## Suggested starting prompt

> A conformance test injects a TCP segment that acknowledges unsent data into an established
> connection. The peer expects an ACK back, but Linux 7.0 stays silent. Find where the segment
> goes and why no ACK is sent.

## Reproduction sketch

1. Run `tools/testing/selftests/net/packetdrill/tcp_ts_recent_invalid_ack.pkt` from the fixed
   tree, or inject a segment with `ack > snd_nxt` into an established connection.
2. Capture the traffic on the guest interface.
3. Break on `tcp_send_challenge_ack` and read `snd_nxt` and `snd_una`.
4. Repeat with the fixed kernel as a second Run on the same System.

## Expected signal on vulnerable kernel

- The capture shows no ACK after the injected segment.
- `tcp_send_challenge_ack()` is never reached for it.

## Fixed-kernel expectation

A challenge ACK appears on the wire, subject to the per-socket rate limit.

## A/B scoring hints

- Award credit for evidence from the capture (the missing packet).
- Award extra credit for citing the drop reason in `tcp_ack()`.
- Penalize if the agent treats the silent drop as correct behavior.

## Caveats

The difference is only on the wire, so the capture is the main evidence.
