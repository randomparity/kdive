# Reboot probe readiness (#3075)

## Scope and evidence

Restore the existing post-reboot readiness contract in the live coverage carrier.
Frozen scope: issue #3075, WORK:SCOPE 6052030236; approved campaign exclusions
remain blind timeout inflation, unrelated provider/host/catalog changes, and
native POWER qualification (#2818). Full-spec M250, one PR.

Two exact-candidate Ubuntu image-smoke runs reproduced the reported failure:
SSH connected during boot, printed a PAM booting message, then exceeded its
120-second subprocess timeout. The second run retained that failure and ran a
read-only diagnostic: SSH was listening at boot +7.75 seconds, while
networkd-wait-online completed unsuccessfully at +127.61 seconds and user
sessions became available at +127.72 seconds. A fresh probe then completed
within the existing 300-second reboot window. This is an escaped per-attempt
timeout, not evidence that the overall readiness budget is insufficient.

`control.power` already documents that job completion does not imply guest SSH
readiness. Generic `image_smoke.ssh` also executes mutating commands; retrying a
timeout there could execute those commands twice. `scenario.probe_new_boot`
has two read-only callers: image-smoke `PROBE` and deep-lifecycle `KERNEL_PROBE`.

## Design

Keep the readiness loop in `scenario.probe_new_boot`. It owns one 300-second
monotonic budget starting when called after the reboot job drains. Each attempt
uses the existing SSH helper with its transport retries disabled through
`deadline_s=0`, and a subprocess timeout capped by the remaining budget and the
existing 120-second attempt limit. Add only a keyword-only `timeout_s=120.0`
argument to the private SSH helper; its ordinary callers retain their behavior.

The readiness loop retries SSH transport exit 255, `TimeoutExpired`, and a
successful read-only probe reporting the old boot ID. Between attempts it
sleeps at most five seconds, capped by the remaining budget. It does not start
an attempt after expiration. Exhaustion fails with an actionable assertion
naming the reboot readiness budget and last observed condition. Other command
exit codes fail immediately. Success requires a nonempty changed boot ID;
malformed successful output fails rather than counting as a new boot.

Document that `probe_new_boot` accepts read-only repeatable probes. Parse through
existing `parse_probe`; leave `ssh_probe` and generic SSH timeout propagation
unchanged. No caller migration, public API, production runtime, dependency,
permission, persistent data or qualification predicate changes are required.
An ADR is unnecessary: this restores the documented carrier readiness contract;
reserved optional ADR-0750 remains unused.

## Global Constraints

Python 3.14 and existing dependencies only. Host x86_64; project targets x86_64
and ppc64le remain distinct. No blind timeout inflation or generic command
replay. Preserve failed evidence and unchanged qualification assertions.

## Success

1. A read-only reboot probe that times out once can observe the new boot within
   the existing 300-second budget; the timeout alone cannot abort readiness.
2. Attempts and retry sleeps respect that budget; exhaustion remains failure.
3. Exit 255 and old boot IDs retry; command errors and malformed IDs fail.
4. A generic SSH command timeout propagates without replay.
5. The unchanged Ubuntu image-smoke cell passes at the deployed fixed candidate,
   retaining both original failed runs separately, with owned cleanup proven.

## Failure model

- **Actors and deployments:** operator/CI live-stack carriers using SSH against
  owned disposable local or remote guests; current reboot callers are read-only.
- **Invariants and assets at stake:** no repeated mutating command after an
  ambiguous timeout; no false new-boot success; finite readiness wait; truthful
  failed evidence and cleanup. Existing SSH authentication options stay intact.
- **Accepted failure classes:** guest never ready within 300 seconds fails;
  timeout kills the local SSH client without claiming guest command termination;
  OS scheduling overhead can add a small amount beyond the configured budget.
  Guest network-online failure itself remains visible and is not repaired here.
- **Covered elsewhere:** provider power semantics by its existing documented
  operation contract; guest configuration by image ownership; native POWER #2818.

## Validation

Focused deterministic tests inject subprocess outcomes and a monotonic clock at
external boundaries, exercising the real SSH/readiness logic. Cover timeout then
new boot, exit 255, old boot, final shortened attempt, bounded sleeps, exhaustion,
command error, missing/empty boot ID, and generic timeout non-replay. Preserve red
before green and mutation evidence. Run focused tests, lint, whole-tree type and
relevant hooks. The installed pre-push hook owns the full ordinary gate.
Deploy the fixed commit to the assigned fixture, attest all three roles, and run
`tests/integration/test_image_smoke_live.py::test_image_smoke[ubuntu-kdive-ready-26.04]`
without the diagnostic observer. A passing retry without the causal fix is not
qualification. No full catalog, native POWER or capture claim follows.
