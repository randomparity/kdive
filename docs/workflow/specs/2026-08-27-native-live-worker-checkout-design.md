# Native live workers execute the tested checkout

## Scope

Fix the native `live_vm` job so it tests the dispatched checkout without granting that checkout
root installation authority. The hosted TCG job keeps installing its lifecycle host contract on
its disposable runner. Runner provisioning, lifecycle request semantics, database schemas, and
worker privileges do not change.

## Design

The persistent runner continues to receive the root lifecycle witness, systemd units, fixed worker
accounts, and dependency environment from Ansible. The native workflow no longer invokes the root
installer. Before `start`, the unprivileged lifecycle client compares an explicit semantic protocol
identifier and a deterministic schema digest with the same values computed by the installed witness
Python environment. Any mismatch fails with an instruction to reprovision the runner. Changes to
request meaning, lifecycle ordering, retries, deadlines, or state reconciliation must bump the
identifier even when the request and response models do not change.

The lifecycle request already accepts an absolute worker executable. For native runs, the client
passes a checkout-owned launcher that prepends the checkout's `src/` directory to `PYTHONPATH` and
then executes the provisioned Python interpreter. Systemd still runs that launcher as the fixed
`kdive-worker-N` account. Hosted TCG keeps using the installed interpreter directly because its
contract was installed from the same checkout earlier in the job.

This design is recorded by [ADR-0582](../../adr/0582-native-live-workers-execute-the-tested-checkout.md),
which supersedes only ADR-0574's exact-revision prerequisite.

## Failure handling

- An inaccessible launcher or checkout source tree fails before a lifecycle request is sent.
- A lifecycle protocol identifier or digest mismatch fails before workers start and directs the
  operator to run the runner Ansible playbook.
- The lifecycle witness continues to validate and bound the existing request. No protocol field is
  added, so a compatible provisioned witness accepts the request unchanged.

## Threat model

### Boundaries and actors

- A trusted workflow dispatcher selects a checkout whose code crosses into an unprivileged fixed
  worker account. That checkout is intentionally the test subject and never crosses into root.
- The checkout-side client sends a bounded lifecycle request over the root-owned systemd socket.
  The existing peer-credential, model-validation, allowlist, and size controls remain authoritative.
- The installed root-owned Python environment is inspected only for a public protocol identifier
  and deterministic schema digest. The probe uses Python isolated mode (`-I`) so neither the
  checkout working directory nor caller-controlled Python environment variables enter `sys.path`.

No new boundary is added to the hosted disposable runner. Anonymous users and pull requests cannot
dispatch the self-hosted job because the workflow has no `pull_request` trigger.

### Controls

- The launcher runs as `kdive-worker-N`; it cannot modify units, credentials, witness code, or
  root-owned lifecycle state.
- The installed probe uses the provisioned interpreter in isolated mode and clears ambient
  `PYTHONPATH`, so the checkout cannot impersonate the installed contract module through either an
  environment entry or Python's ordinary current-working-directory entry.
- Exact protocol-identifier and digest equality rejects semantic and structural incompatibility
  before mutation.
- Existing absolute-path, worker-account accessibility, socket metadata, request-size, deadline,
  and response-validation checks remain in force.

### Out of scope

- A trusted dispatcher deliberately selecting malicious code can execute it with the fixed worker
  account's documented libvirt and provider-directory authority. That is the purpose and existing
  trust model of this test runner; the change does not grant root authority.
- Updating an incompatible installed lifecycle protocol remains an operator provisioning action.

## Verification

- A workflow-shape regression proves the native job contains no privileged installer and exports
  the checkout launcher for its spine.
- Lifecycle-script tests prove matching protocol identifiers and digests proceed, either mismatch
  fails before a request, and installed compatibility data ignores both ambient `PYTHONPATH` and a
  hostile checkout-root shadow module.
- Launcher tests prove the provisioned interpreter receives the checkout `src/` first and preserves
  the worker command arguments.
- The relevant shell, workflow, unit, type, and focused test guardrails run locally. The native KVM
  job is the end-to-end proof and must be rerun on the self-hosted runner after merge or dispatch.
