# 0589 — Remote module appliance console wait is idle-bounded

## Status

Accepted (2026-09-04)

## Context

ADR-0585 permits 200,000 entries and 8 GiB of module content and gives an appliance invocation
300 seconds. The pending appliance supervisor instead treated 30 seconds as one total console
wait, even when the appliance continued to report progress. It also retried libvirt's nonblocking
would-block result without yielding. A full 8 GiB operation completed inside 300 seconds requires
at least 28,633,116 bytes/s (about 27.3 MiB/s) including filesystem work. That rate is plausible
for host-local libvirt volumes but is not guaranteed by the remote control connection.

## Decision

The 30-second console bound is an idle bound. Each non-empty console chunk re-arms it from the
monotonic clock, capped by the invocation's fixed 300-second deadline. Empty/end-of-stream values
do not re-arm it. A would-block result sleeps for 10 milliseconds, bounded by the current idle
deadline, before another read; it is not progress and does not re-arm the deadline.

The 8 GiB figure remains an accepted-content ceiling, not a promise that the slowest conforming
operation finishes in one invocation. The 300-second cap bounds one invocation across the remote
libvirt control link and host-local appliance I/O. Timing out leaves durable appliance evidence
for the existing retry/recovery path. Tests assert that chunks arriving every 20 seconds permit a
run beyond 30 seconds, no chunk permits more than 30 idle seconds, and the total never exceeds
300 seconds.

## Consequences

Healthy long-running appliances are not destroyed solely because total elapsed time crossed 30
seconds. A silent or stuck appliance remains bounded, and repeated would-block reads do not occupy
a CPU in a tight loop. Hosts unable to process an admitted tree within 300 seconds may require a
retry; changing that outer budget or the content ceiling remains a separate decision.

## Considered & rejected

- **Keep 30 seconds as the total wait.** verified: issue #2169 identifies that the existing loop
  computes its deadline once, so a chunk at 20 seconds cannot extend the run to receive another at
  40 seconds; this contradicts the required progress-sensitive behavior.
- **Shrink the 8 GiB content ceiling to a rate-derived value.** judgment: the provider has no
  guaranteed minimum host-storage throughput from which to derive a portable smaller ceiling, so
  such a number would imply a guarantee the deployment contract does not make.
- **Let every would-block read retry immediately.** verified: libvirt uses `-2` as the nonblocking
  would-block sentinel and the pending implementation immediately continues, creating a tight loop
  until its deadline.
