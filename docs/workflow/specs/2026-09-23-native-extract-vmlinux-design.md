# Native warm-store extractor provisioning

Issue: #2661. Status: reviewed design. Base: `main`.

## Problem and boundary

A cold native warm-store refresh reads the guest's compressed kernel, then calls
`extract-vmlinux` to obtain its ELF build-id. A clean Ubuntu 26.04 host can have the matching
extractor in its running-kernel headers but no command on `PATH`, so the rebuild fails after
rootfs work. A cached warm set bypasses this path. The Ubuntu runner imports the Debian worker
package task, which currently omits the headers and command.

## Required behavior

On Ubuntu, the shared Debian worker package task installs headers for the running kernel,
requires an executable extractor in those headers, exposes it as `extract-vmlinux` on the
operator's `PATH`, and verifies the operator resolves its executable path. A fresh rebuild
proves actual invocation. The runner's import binds the
shared role's operator variable to its own runner account; the standalone role already
requires its operator variable. The role fails with an actionable
message when a matching header package or extractor is unavailable. The symlink is refreshed by
reapplying the role after a running-kernel change; the runbook names this maintenance step.

Only a cold native warm-store rebuild checks for the command before the expensive rootfs build.
A warm set remains consumable without it. A compressed kernel continues to use the existing
`kernel_build_id` extraction; the hosted TCG and bare-ELF paths do not change.

## Failure model

- The operator owns Ansible preparation; the unprivileged runner owns warm-store refresh. The
  role must not validate only root's ability to execute the script. The runner importer must
  pass the operator account into the shared task.
- A missing header package fails at package installation. A package with no executable script
  fails at the role assertion. A removed or inaccessible command fails in the cold-store
  preflight before rootfs construction. Reapplying the role repairs a stale symlink after a
  kernel upgrade.
- The role changes only Ubuntu package state and `/usr/local/bin/extract-vmlinux`; other Debian
  family hosts retain their existing package contract. Existing store data is untouched.

## Scope and validation

The change touches the shared Debian package task, its defaults, `warm-store.sh`, focused tests,
and the native runner runbook. It excludes #2657 readiness/timeout, #2658 candidate/publication,
and hosted ppc64le bare-ELF behavior. It is separate from epic #2655. No ADR or migration is
warranted: the existing host prerequisite is being provisioned, not changing a public contract.

`focused-test`: a cold-store stubbed test must fail before calling the rootfs builder when
`extract-vmlinux` is absent, while a valid warm store does not require it. The test must be red
before implementation. A role-focused check must prove Ubuntu installs the versioned package,
points the command at its script, and validates as the operator; the Ansible lint gate checks
syntax. A fresh rebuild on a prepared Ubuntu x86_64 KVM host must complete from an empty
disposable store and report the artifact/build-id evidence. No hosted TCG run is required.
