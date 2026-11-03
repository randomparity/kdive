# Design — the guest-agent observation cap leaves room over live boots (#3421)

## Problem

`_OBSERVE_WAIT_S` capped the remote running-kernel observation at 90 s. Live boots connect the
guest agent 50 to 54 s after the domain starts, so a slower host failed with `READINESS_FAILURE`
and the failure did not say the cap was the limit.

## Decision

- Raise `_OBSERVE_WAIT_S` to 300 s, about five times the observed connect time. The caller's
  deadline stays the other bound in the `min`, so the wait still never outlives the job.
- A cap derived only from the caller's deadline is rejected: that deadline spans the whole job, and
  a guest that never answers would hold the System's lane for all of it.
- An operator setting is out of scope (operator-approved exclusion); `_OBSERVE_POLL_S` is unchanged.
- The `READINESS_FAILURE` details carry `observe_wait_cap_s`, the cap in force.

## Verification

`tests/providers/remote_libvirt/test_external_boot_observe_readiness.py`: an agent connecting at
120 s (past the old cap, inside the new) is observed; the cap test asserts the wait ends at the cap constant and that
the failure details name it.
