# Native remote ppc64le vmlinux staging and restore

Date: 2026-11-04. Tracks #3512 and the remote-authority arm of #3131.
The unchanged live carrier passed on native POWER/KVM-HV: **1 passed in 609.81 seconds**.
The enclosing process exited 0 after 610.923 seconds measured by controller monotonic time.

## Candidate and topology

Both source checkouts, the controller server/reconciler/fixed worker, and the installed remote
provider authority matched `7be2770516a81684ae5325179e54efbadae59361` before invocation.
The Linux ARM64 controller and native POWER authority used separate filesystems. The controller
ran the portable host stack with one lifecycle-managed fixed worker, Postgres, SeaweedFS and
the mock issuer. The authority owned libvirt, preparation and recovery. Existing SSH host-key
verification, mutual TLS and authority-derived SNI remained enabled.

The controller had 1,944,416 KiB available memory after stack startup, plus its existing swap.
An actual active System observation recorded `type=kvm`, `arch=ppc64le`, `machine=pseries-10.2`,
`kvm_hv` reference count 1 and no loaded `kvm_pr`. This was execution evidence from the same
carrier, separate from the preflight domain-capability check.

## Pinned inputs

The complete kernel bundle was built natively on POWER and uploaded through the existing
prebuilt-bundle path. The ARM controller generated no foreign initrd. Its minimal input tree
has no Makefile, so the generic header reports release unavailable; the explicit release file,
archive release, matching ELF build identity and server validation supplied that provenance.

| Input | Identity |
|---|---|
| Linux source commit | `3cb1fb7a56d2fd8011f5282bc170c0d23dc1f4b5` |
| Release | `7.0.1` |
| GNU build ID | `2a2a2e7a66c36ef951431b3cceee9ec1265a6b28` |
| Root filesystem requirement | btrfs; `CONFIG_BTRFS_FS=y` |
| Unstripped vmlinux | 433,015,104 bytes; SHA-256 `346d5d5c431ab53e9cbb2f9edc5f1894db09670f14eb96e793ba057d8e53a00c` |
| Config SHA-256 | `a26b9a62049866f756ef1784b5bc17c0496353648d75406c2d11030e27356366` |
| Kernel bundle SHA-256 | `6cd291d21e234e5f44294c3816f9681fead280766699ba141cde2f3449c47ce7` |
| Initrd SHA-256 | `88c83f7d9fd14594d587f859d74eca10e7c589c5850fd40a30f4091898950e11` |
| Customized Fedora 44 POWER base SHA-256 | `d8c97e0a37de8e768d3a567d281da43542adf7cbc7799f555d7699bd7df6621a` |
| Production appliance bundle SHA-256 | `76897ea12d83c0238ae0547d8cdba8dda481abe9902ee38035597d15902bc8d6` |
| Appliance manifest digest | `sha256:4f467cb6a579a967e65b810960e5dc27d85bc43094f973b4372ab1664ed4bade` |
| Appliance kernel SHA-256 | `56142ec1ca85a3bf5ecac99a075dd2646291894496e97305571f8b461bceed44` |
| Appliance initramfs SHA-256 | `197af8e29904852d28b2f2efed7897a972d1c77d2e7529a159a21bab618321c9` |
| Appliance init SHA-256 | `fb117ae66bb292d5799c633bedc7fe654c1352ae2900dc9490d028627e2c0ebf` |

The stock builder and installed loader verified the image. All 202 runtime records and the
appliance kernel matched the retained stock image; only the intended init changed. The proof
used the production appliance without diagnostic instrumentation. Existing 30-second console
idle and 300-second host invocation bounds, and the appliance's 256 MiB/1 vCPU, were unchanged.

## Invocation and observations

With the installed lane, harness database authority and native bundle configured, the invocation
was `python -m pytest tests/integration/test_authority_vmlinux_live.py -vv -s --tb=short`, with
private temporary and JUnit destinations. The carrier selected ppc64le from actual remote
inventory; it needed no per-architecture keyword filter.

The carrier observed all of these through the real public MCP lifecycle and guest agent:

- The running kernel reported `7.0.1`.
- `/lib/modules/7.0.1/vmlinux` matched the uploaded SHA-256 and 433,015,104-byte input.
- Real `drgn -k`, using default symbol search and reading `init_uts_ns`, resolved `7.0.1`.
- Ordinary external-boot release completed. A baseline reboot reported
  `6.19.10-300.fc44.ppc64le`, and the staged vmlinux was absent.
- Normal frame teardown reached `torn_down` in 35 seconds and released the allocation.
  The carrier compared before/after authority inventory and capacity; in-use capacity returned
  to 0. Separate post-run SSH/libvirt inventory showed no domains and only the pinned base,
  whose full SHA-256 was unchanged. A fresh public availability read independently reported 0
  in use; database inspection found no nonterminal jobs, active allocations or live Systems.

The run emitted one disclosed `UserWarning`: the Kubernetes-only lifecycle-witness was not
installed in the portable three-role stack. Server, worker and reconciler revisions matched.
This is not a claim of a fourth deployed role or of a warning-free live invocation.

## Earlier failures and limits

Earlier carriers remain failed. Native provisioning exposed x86-only domain features and the
implicit pSeries panic device. A later installed scratch result exposed the invalid physical
allocation versus logical capacity assumption. The branch fixes those specific contracts.
Two failed attempts required separately authorized, strictly guarded operator reaping before
ordinary public cleanup could finish; that remediation was not a passing carrier or a new
product recovery behavior.

An isolated diagnostic was interrupted by a storage/filesystem failure, with exit 120 and an
empty supervisor trace; its timeout cause remains unknown. Subsequent diagnostic copies were
contaminated by offline rebinding before journal recovery and are excluded from product
conclusions. Corrected diagnostics established idle expiry during useful silent producer work,
including an unfinished destination hash; instrumentation could affect timing. Those diagnostics
motivated bounded producer liveness through the existing advisory, without changing timeout
policy. The successful carrier above is a separate production-image run.

Protected failure evidence and native kernel/base/bundle/appliance fixtures were retained.
This record proves the native remote-authority arm. It does not replace the local-authority
staging arm, legacy remote delivery, other parent criteria, final branch review, ordinary
pre-push gate or CI.
