# Native worker readiness before System mint

Issue: #2657. Status: reviewed design. Base: `main`.

## Failure and boundary

A system library was replaced after the fixed worker's root-owned capture bootstrap manifest was
installed. The fixed worker's `/readyz` reported `capture_bootstrap_manifest=false`, so its claim
loop skipped dequeue. The provision job remained queued with zero attempts while the native spine
polled `systems.get` for about 15 minutes. Reapplying the runner Ansible playbook rebuilt and
verified the manifest; the next native job claimed the provision job and passed 14 proofs.

## Required behavior

After native stack bring-up and before `mint-system.sh`, the workflow checks the actual fixed
worker's loopback `/readyz`. It accepts only the allowlisted component booleans emitted by
`filter-worker-readiness-evidence.py`; the filter discards the build payload and any unexpected
response shape. A ready worker proceeds to mint. A temporarily unready worker is retried at a
modest interval for about one minute. If it remains unready or the response cannot
be read or validated, the workflow fails before creating a System and reports the last safe
readiness line (if any) plus the runner correction runbook. The workflow does not regenerate the
manifest, acquire privilege, or override worker readiness.

If mint later fails for another reason, the existing post-failure diagnostic still emits a
redacted persisted System/job state and the fixed worker's safe readiness components. Job,
System, worker, host, and network identifiers must not appear in public diagnostics.

## Failure model

- Actors and deployments: the native CI operator starts the host stack, while the fixed systemd
  worker owns job dequeue and serves loopback readiness. The installed worker and checked-out
  workflow can have different source revisions.
- Invariants and assets: only a ready fixed worker may precede System mint; the root-owned
  capture manifest remains privileged host state; diagnostics expose component booleans without
  identifiers, paths, exceptions, or build identity.
- Accepted failures: a short startup delay may consume retries; a persistent worker failure
  stops the native tier before mint, preserving a clear red result. A later independent provision
  failure still uses the persisted job/System capture.
- Covered elsewhere: the runner Ansible role installs and verifies the manifest, the worker
  enforces readiness at dequeue, and the existing filter validates the readiness payload.

## Scope and validation

The implementation is confined to the native workflow, its fixed-shape readiness filter and
workflow-shape tests, and the self-hosted runner runbook. Version/compatibility notes belong to
#2656; combined release-candidate validation belongs to #2658; remote and TCG provider behavior
is excluded. No schema, public tool contract, or privileged host installation changes.

Focused workflow/filter tests and the ordinary PR gate cover syntax and ordering. A native KVM
run on the final head must reach pytest with throwaway, provisioned, and debug-stepping fixtures
configured, report its pass/skip counts and exit status, and show the fixed worker ready. The
documented Ansible correction is verified separately as the fixed worker before that live run.
