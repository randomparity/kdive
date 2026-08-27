# 0582 — Native live workers execute the tested checkout

## Status

Accepted (2026-08-27)

## Context

ADR-0574 provisions a root lifecycle witness and unprivileged fixed worker accounts on the
persistent native KVM runner. Its client also required the installed witness Git revision to equal
the test checkout. Issue #2050 attempted to maintain that equality by invoking the root installer
from the native workflow. Run 33094018806 showed the runner correctly rejects that `sudo`; allowing
it would also contradict ADR-0574's explicit rejection of job-time privilege escalation.

The worker must nevertheless execute the dispatched checkout. Executing only the Ansible-installed
KDIVE package can make a green native run prove an older revision.

## Decision

The native job does not install privileged lifecycle state. Ansible remains the only owner of the
persistent runner's witness, units, fixed Python environment, and credentials.

Before worker start, the unprivileged client compares an explicit semantic protocol identifier and
deterministic digests of the checkout and installed lifecycle request/response schemas. Changes to
request meaning, lifecycle ordering, retries, deadlines, or state reconciliation must bump the
identifier even when the models remain unchanged. Any identifier or digest mismatch fails before a
request and requires runner reprovisioning. This replaces ADR-0574's exact-Git-revision prerequisite;
its other decisions remain in force.

The check gates `start`, `status`, and `stop`, because status reconciliation can publish termination
evidence. It does not gate observational `diagnostics`, which remains available to explain a
mismatched installation.

The installed half of that comparison runs through the provisioned interpreter in Python isolated
mode. The checkout working directory and caller-controlled Python environment therefore cannot
shadow the root-installed contract module used to compute compatibility.

For a compatible protocol, the client supplies a checkout-owned executable through the existing
bounded worker `python` setting. That launcher prepends the checkout's `src/` to `PYTHONPATH` and
executes the provisioned interpreter. The root witness still controls lifecycle and credentials;
systemd executes checkout application code only after dropping to `kdive-worker-N`.

## Consequences

Native runs test checkout worker code without giving checkout code root execution. Compatible
application changes need no host reprovision. The runner needs one Ansible reprovision to acquire
the compatibility identifier; later lifecycle protocol changes fail clearly until it is
reprovisioned again. The hosted TCG runner continues installing the exact contract because it is
disposable and already grants setup steps sudo.

## Considered & rejected

- **Install from the native job.** verified: run 33094018806 job 98594077650 failed at the new
  installer with the runner's sudo refusal; ADR-0574 also records that job-time sudo is outside the
  persistent runner's trust model.
- **Require exact revision and reprovision before every run.** judgment: this makes ordinary
  application-only commits depend on privileged host deployment and leaves scheduled runs stale by
  construction.
- **Expose a privileged checkout updater over sudo or IPC.** judgment: accepting a workflow-selected
  source tree for root installation recreates the authority ADR-0574 deliberately excludes.
