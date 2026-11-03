# Final kernel retention for the Fedora regression image

## Authority and verified problem

Approved (2026-10-08). The operator approved the replacement field and bounded RPM retention
policy in ADR-0761. The cloud-init directory fix remains authorized; ADR-0760 preserves the
superseded customization-only decision. The carrier and provisioning selector remain unchanged.
The final image at `258221bb` built successfully in 83.4s but its mandatory unhinted smoke failed
in 17.50s with two kernels. Read-only inventory and the actual selector reproduce the ambiguity.
No ready, authenticated-access or reboot success is inferred from that build.

## Contract and ownership

Replace unmerged RootfsCatalogEntry.customization_kernel with optional retained_kernel, default None.
Use the same nonempty-string validation and exact full-filename/bare-version selector. Reject
customization_kernel if present with an actionable replacement-field error; do not ignore it.
A present retained_kernel is allowed only when distro=fedora and arch=x86_64; unsupported opt-ins
fail catalog loading before acquisition. Omission preserves current behavior for other rows,
including POWER. Set only fedora-kdive-ready-43 to 6.18.5-200.fc43.x86_64.

The existing rootfs builder remains the owner: forward the field to the existing transient
extractor, then append the retention RunCommand after normal family exec steps. Add no Step type,
provider port, profile field, generic package-policy framework or compatibility alias.
The internal unmerged caller/test/docs migration removes the old name; no persisted record uses it.
Historical ADR-0760 stays intact apart from a later authorized supersession status annotation.

Normalize the selected version by removing an optional vmlinuz- prefix after the existing selector
has validated it. Shell-quote it. Inside the booted build guest require uname-r and installed
kernel-core VERSION-RELEASE.ARCH to equal that version. Capture rpm's full query output with its
exit status before parsing. Build a positional-argument list of other-release NEVRAs only for
kernel/kernel-core/kernel-modules-core/kernel-modules/kernel-modules-extra. No package is chosen
by ordering. Run rpm --erase --test followed by rpm --erase on that exact list when nonempty;
keep dependency checking and scriptlets enabled. An error propagates through the current
firstboot failure marker and prevents publication. No host RPM database or source template is modified.

After customization and before seal/publication, call the existing boot-inventory probe strictly,
then existing select_kernel_and_initrd without a hint. Require the lone selected filename and
its matching initramfs. This required check must not use the advisory exception-swallowing
_capture_boot_facts path. Existing provenance subsequently records the actual final image normally.
The failed scratch image remains disposable; no artifact is published on strict-check failure.

## Success

The selected Fedora43 row produces one selected non-rescue kernel with matching initramfs,
preserves selected runtime packages and unrelated packages, and passes the unchanged native
image-smoke cell through first boot, SSH, OS/architecture identity, reboot and cleanup.
The rootfs build and deployed roles must match the candidate. Existing multi-kernel/no-hint
provisioning still refuses ambiguity; no carrier hint or manual image modification supplies a pass.

## Failure model

- Actors: operator catalog, vendor template/packages and the existing isolated customization guest.
- Assets/invariants: selected kernel and dependency closure, RPM consistency, bounded disposable
  image mutation, unchanged source input, fail-closed publication and truthful proof identities.
- Accepted: vendor package/scriptlet drift can fail the build; the remedy is evidence and a new
  reviewed disposition, not broader erasure. Bit-identical packages are not promised. Rescue files
  are outside the non-rescue selector contract. Native POWER is not tested by this x86 proof.
- Required handling: malformed/obsolete fields, unsupported opt-ins, query/transaction failures,
  running-kernel mismatch, dependency refusal, missing selected kernel/initramfs, extra candidate
  and inspection failure stop without publishing. Do not weaken RPM checks or guest authority.

The observed database has four runtime packages for each version. RPM's dry-run refuses old-core
alone and accepts the four-old-package set; selected core requires matching modules-core.
The old core's recorded preuninstall script calls kernel-install remove for its exact version.
Those are metadata/transaction-plan observations, not proof that live scriptlets or reboot work.

## Validation and exclusions

Catalog tests cover replacement, omission, retired key, bad type/empty and unsupported distro/arch.
Builder tests exercise existing selector logic for the two-candidate input and strict final checks.
Execute the generated shell with stub external uname/rpm tools: exact obsolete transaction args,
selected/unrelated retention, no-op, query failure, dependency refusal and erase failure.
A controlled missing retention step must leave the two-candidate final check red.
Run focused family/catalog/build tests, lint, whole-tree types and relevant shell/docs checks.
After explicit approval, rebuild through the real catalog command and record actual package/boot
inventory and provenance. Then rerun the unchanged mandatory native image-smoke cell on an
exclusively assigned host with matching deployed roles; no host is presently assigned.
Preserve failed prior evidence. Native POWER #2818 and regression catalog removal remain excluded.
Final review budget remains 1/2 used. No push, implementation or host mutation at this design checkpoint.
