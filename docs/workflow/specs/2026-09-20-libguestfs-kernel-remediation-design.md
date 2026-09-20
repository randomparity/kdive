# Libguestfs kernel-readability remediation

Issue: [#2594](https://github.com/randomparity/kdive/issues/2594). This is a full-spec, S-sized
repair; the fixed 100-line denominator comes from the frozen issue assessment.

## Problem

The shared libguestfs diagnostic still tells operators to widen kernels to `0644`, repeat that
after upgrades, or use a globbed `dpkg-statoverride`. Local-worker provisioning instead keeps
Debian-family kernels `root:kvm 0640` and installs a post-install hook. The preflight retains an
obsolete manual-upgrade phrase. ADR-0222 made the former remediation choice, so it needs a narrow
successor rather than a decision rewrite.

## Scope

Change only the diagnostic and local preflight wording, their focused tests, ADR-0669, and the
ADR-0222 status amendment. The Debian/Ubuntu shared diagnostic leads with local-libvirt preparation:
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL=... just prepare-local-libvirt-host`; other deployments use
their owning provisioning play. It states `root:kvm 0640`, requires the worker to belong to `kvm`,
and says provisioning installs the durable upgrade hook. Its explicitly temporary fallback is
`sudo chgrp kvm` then `sudo chmod 0640` over `/boot/vmlinu?-*`. No matcher, category, passt text,
role, hook, platform guard, loop, existing host, hosted-runner exception, or cross-language
abstraction changes.

## Failure model

- **Actors and deployments:** local/remote image-build operators receive the shared diagnostic;
  local-libvirt operators receive the preflight; tests exercise injected stderr and stubbed paths.
- **Invariants and assets:** only `kvm` readers gain kernel access; the worker must belong to that
  group; the recognized kernel stderr remains `CONFIGURATION_ERROR`; the local recipe is not
  described as universal.
- **Accepted failure classes:** an operator who chooses the fallback must repeat it for a later
  kernel; this is explicit because only provisioning installs the hook. Other deployment play
  names are deployment-owned configuration, not a local-libvirt inference.
- **Covered elsewhere:** hook, Debian-family scope, and relabel mechanics are ADR-0575/ADR-0668;
  the ephemeral hosted CI exception remains `.github/workflows/live.yml`.

## Design

Keep `_KERNEL_REMEDIATION` as a single constant consumed by the existing regex path. Replace only
its text with the supported-provisioning-first contract and safe temporary command. Correct the
same preflight contract in-place. ADR-0669 supersedes ADR-0222 only for this remediation decision;
the old ADR receives a Status pointer and retains matcher, classification, stderr, and passt text.

## Success

- For kernel-unreadable stderr matches, the existing message, matcher, and `CONFIGURATION_ERROR`
  path retain the Debian/Ubuntu qualification and return remediation mentioning the supported local
  command, `root:kvm 0640`, `kvm` membership, the durable hook, deployment-owned alternative
  provisioning, `/boot/vmlinu?-*`, `sudo chgrp kvm`, and `sudo chmod 0640`.
- The affected image and script tests reject `0644` and `dpkg-statoverride`; the script test also
  proves the both-architecture glob and durable-hook qualification.
- ADR-0669 records the replacement choice and ADR-0222 has only its supersession pointer changed.

## Validation

- `tests/images/planes/test_build_common.py`: injected matching stderr proves unchanged category
  and the replacement remediation contract; focused command:
  `just test-verbose tests/images/planes/test_build_common.py::test_run_guestfs_tool_maps_unreadable_host_kernel_to_configuration_error`.
- `tests/scripts/test_check_local_libvirt.py`: unreadable boot fixture proves the safe, durable
  preflight hint; focused command:
  `just test-verbose tests/scripts/test_check_local_libvirt.py::test_unreadable_host_kernel_fails_with_chmod_hint`.
- ADR prose: `just records origin/main` validates record structure; no task-specific executable
  observation applies to a narrow accepted-decision supersession.
