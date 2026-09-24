# TCP_USER_TIMEOUT ignored during zero-window probing

## Summary

- **Subsystem**: networking, TCP timers (`net/ipv4/tcp_timer.c`)
- **Fix reference**: [2b9f6f7065d4](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=2b9f6f7065d4cfb65ba19126e0b35ac4544c3f3a) "tcp: make probe0 timer handle expired user timeout"
- **Introduced by**: 344db93ae3ee "tcp: make TCP_USER_TIMEOUT accurate for zero window probes" (v5.11-rc6)
- **Fixed release**: Linux 7.1-rc2
- **Architecture**: generic
- **Primary symptom**: Connection teardown happens long after `TCP_USER_TIMEOUT`
- **VM suitability**: Medium. Deterministic, but each attempt takes tens of seconds.
- **KDIVE tools exercised**: `control.capture_traffic`, `debug.set_breakpoint`, `debug.read_registers`, `debug.set_watchpoint`, `introspect.run`

## Bug description

`tcp_clamp_probe0_to_user_timeout()` computes the remaining time in an unsigned variable. When
the elapsed probing time is past the user timeout, the subtraction wraps to a large value, and
the probe timer is armed for a full backoff interval instead of expiring.

## Suggested starting prompt

> A client sets `TCP_USER_TIMEOUT` to 5 seconds, but when the peer advertises a zero window,
> the connection stays open far longer. Find why the timeout is not enforced.

## Reproduction sketch

1. Connect, set `TCP_USER_TIMEOUT` to about 5000 ms, and keep writing.
2. Make the peer stop reading so it advertises a zero window.
3. Capture the traffic and time the probes and the final reset.
4. Break in `tcp_clamp_probe0_to_user_timeout` and read the computed remaining time.

## Expected signal on vulnerable kernel

- Zero-window probes continue with growing backoff past the configured timeout.
- The computed remaining time is a very large unsigned value.

## Fixed-kernel expectation

The connection fails soon after the configured timeout.

## A/B scoring hints

- Award credit for timing evidence from the capture.
- Award extra credit for finding the unsigned wrap.
- Penalize if the agent only tunes retry sysctls.

## Caveats

Slow per attempt; budget several minutes for the A/B pair.
