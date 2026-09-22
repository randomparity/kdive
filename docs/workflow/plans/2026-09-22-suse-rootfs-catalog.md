# SUSE rootfs family and openSUSE catalog images — implementation plan

**Goal:** Implement issue #825's approved distro-native SUSE family, add pinned Tumbleweed and
Leap 15.6 debug images, and prove each image reaches KDIVE's existing incomplete-core remediation
for the v7.0 test kernel.

**Architecture:** Extend the ADR-0251 `FamilyCustomizer` registry with a debug-only SUSE adapter.
Keep SUSE package, service, sysconfig, and partial-core behavior inside that adapter. Represent an
explicitly unavailable catalog tool with the required `"absent"` sentinel, parsed as `None`; reuse
the existing computed capability and local-libvirt harvest contracts.

**Tech stack:** Python 3.14, pytest, TOML, zypper, systemd, dracut/kdump, libguestfs, libvirt,
FastMCP live-stack harness, `uv`, and `just`.

Revised implementation size: 2000–2300 changed lines (L), including the design artifacts, family
tests, catalog snapshots, baseline-initrd support, the bounded initrd transform, two live
parameters, and documentation. The original 700–1000 L estimate did not include the boot and
network compatibility work exposed by the real images or the negative-path live-helper coverage.

## Constraints

- Base branch: `main`; branch: `feat/suse-rootfs-825`; sibling worktree only.
- Frozen scope token: `q825-eb2c81ec`; latest complete `WORK:SCOPE` comment governs.
- Official distribution image and package repositories only.
- x86_64 debug images only; no SUSE build-host or ppc64le row.
- Preserve RHEL/Debian behavior and the existing provider-side incomplete-core contract.
- Public artifacts contain no private host, user, network, serial, or location identifiers.

## Task 1: Add explicit absent-tool catalog evidence

**Files:** modify `src/kdive/images/rootfs/catalog.py`,
`tests/images/test_rootfs_catalog.py`, and, only if direct consumers require typing updates, their
focused tests.

**Interface:** `RootfsCatalogEntry.drgn_version` becomes `str | None`. The required TOML value
`drgn_version = "absent"` parses to `None`; missing, empty, or non-string values remain
configuration errors. Every existing version string is unchanged.

**Steps**

1. Add focused tests proving the sentinel maps to `None`, field omission still fails, and normal
   versions remain strings. Confirm the sentinel test fails before implementation.
2. Implement the minimum parser branch and update the dataclass/docstring.
3. Run `just test-verbose tests/images/test_rootfs_catalog.py`, `just lint`, and `just type`.
4. Commit as one conventional `feat` commit after the focused checks pass.

Acceptance: absence is explicit in TOML and typed as absence downstream; accidental omission does
not become valid.

Rollback: revert the task commit; no persisted or external data changes.

## Task 2: Implement the debug-only SUSE family

**Files:** add `src/kdive/images/families/suse.py` and
`tests/images/families/test_suse.py`; modify `src/kdive/images/families/__init__.py` and
`tests/images/families/test_capability_evidence.py`.

**Interfaces:** register `family_for("suse")`. `SuseFamily.packages()` and `.capabilities()` accept
the two approved distro identities for debug images and raise `CONFIGURATION_ERROR` for build
images or unknown SUSE distros.

**Steps**

1. Add red tests for registry resolution; Tumbleweed/Leap package divergence; debug-only and
   known-distro guards; zypper refresh-before-install; `sshd.service` and `kdump.service` enable;
   NMI sysctl; cloud-init seed; conditional drgn helper/marker; common makedumpfile marker;
   readiness upload; and AppArmor normalization.
2. Add post-capture-script tests for exact success detection, fixed-root/direct-child selection,
   zero and multiple candidates, fixed-name incomplete rename, sync/unmount/poweroff, and
   `/etc/sysconfig/kdump` wiring through `KDUMP_REQUIRED_PROGRAMS` and a no-argument
   `KDUMP_POSTSCRIPT`. Retain vmcore-conversion stderr through SUSE's supported
   `MAKEDUMPFILE_OPTIONS` field, and require either exact unsupported/incomplete warning to
   override a successful exit status. Model both Leap's direct execution and Tumbleweed's shell
   evaluation and require each to invoke the same helper path. Pin the rendered order so kdump
   configuration occurs after package installation, and require every external command the
   script calls to be included in the capture initramfs.
3. Implement the package sets, capabilities, ordered steps, post-capture script, sysconfig update,
   and guestfish normalization. Reuse existing shared constants and helper-step functions; do not
   change the RHEL or Debian classes.
4. Change the registry evidence matrix from an unconditional family × kind product to explicit
   supported cases. Add both SUSE distro cases and keep both kinds for RHEL and Debian. Extend the
   package-evidence rule only if the verified SUSE package name requires it.
5. Run `just test-verbose tests/images/families/test_suse.py
   tests/images/families/test_capability_evidence.py`, then `just lint` and `just type`.
6. Make controlled local faults in the conditional drgn capability and incomplete-rename script,
   observe their focused tests fail, restore, and rerun green.
7. Commit the family and focused tests as one conventional `feat` commit.

Acceptance: the two supported debug package/capability contracts are distinct and fully evidenced;
partial SUSE output is adapted to the existing harvester contract without provider changes.

Rollback: revert the task commit; the registry again rejects `suse`.

## Task 3: Add pinned openSUSE catalog rows

**Files:** modify `fixtures/local-libvirt/rootfs_catalog.toml`,
`tests/images/test_rootfs_catalog.py`, and `tests/images/test_catalog_resolver.py` if its
catalog-derived build cases require expansion.

**Steps**

1. Resolve dated official x86_64 Minimal VM Cloud files for Tumbleweed and Leap 15.6. Download the
   distributor checksum metadata and image, verify the image digest locally, and record the exact
   URL and SHA-256. Do not use rotating aliases.
2. Add the two debug rows with `family = "suse"`, verified makedumpfile evidence, Tumbleweed's drgn
   version, and Leap's explicit `drgn_version = "absent"`.
3. Extend catalog snapshot tests, cloud-source pin tests, family identity tests, the v7.0 kdump
   capability set, live-drgn expectations, and the deliberate absence of SUSE ppc64le rows.
4. Add a catalog-to-family parity test requiring drgn evidence and `Capability.DRGN` to agree for
   every shipped row.
5. Run `just test-verbose tests/images/test_rootfs_catalog.py
   tests/images/test_catalog_resolver.py`, `just lint`, and `just type`.
6. Commit the rows and catalog tests as one conventional `feat` commit.

Acceptance: both names resolve to official digest-pinned x86_64 cloud sources; catalog evidence and
family capability declarations agree; both compute kdump-incapable for v7.0.

Rollback: revert the task commit; no published image row or database migration is involved.

## Task 4: Build and diagnose both images on KVM

**Files:** implementation fixes remain limited to the rootfs build and SUSE family surfaces unless
a directly exposed prerequisite is missing; update provisioning ownership in the same change if
the live build proves a host dependency is undeclared.

**Steps**

1. Confirm the selected development system is x86_64, has writable KVM, libvirt, qemu-img,
   guestfish, Python guestfs bindings, `uv`, sufficient disk, and the repository's declared host
   prerequisites. Install/setup only through documented project provisioning when required.
2. Deploy the exact branch checkout and run `python -m kdive build-fs` for each catalog name with
   distinct output paths. Capture unredacted logs only in private temporary storage.
3. Inspect the produced provenance and require the expected distro identity, installed package
   set, makedumpfile version, AppArmor posture, and conditional drgn version/capability.
4. If a build fails, use the detect-curse workflow before changing code. Add focused coverage for
   every verified code defect and rerun only the affected image until both builds complete. The
   real builds require three compatibility corrections: recognize SUSE's
   `/boot/initrd-<kernel-version>` name; remove Tumbleweed's fixed KIWI repartition hook through a
   2 GiB-bounded streaming zstd/newc transform after the whole-disk repack; and configure the
   predictable `eth0` cloud-init connection identifier for both rows.
5. Record artifact digests and exact source commit for the later live proof without publishing
   private machine paths.

Acceptance: both real images finish customization and sealing from the pinned inputs; their
provenance matches the package/capability contract.

Rollback/cleanup: remove only quest-created temporary build products after proof artifacts are no
longer needed; preserve logs needed to explain a failure.

## Task 5: Add and run the two lifecycle proofs

**Files:** modify `tests/integration/test_live_stack.py` and its focused support tests only if a
small shared job-failure assertion is extracted; modify the live-testing/image-lifecycle runbook
for the new environment variables.

**Interfaces:** add `KDIVE_GUEST_IMAGE_SUSE_TUMBLEWEED` and
`KDIVE_GUEST_IMAGE_SUSE_LEAP_15_6`. Each variable selects one independently skippable live
parameter.

**Steps**

1. Add the two named image cases to the SSH-reachability proof without weakening the existing
   Debian/RHEL cases, then invoke `systems.authorize_ssh_key` and require its terminal success.
   Keep ADR-0294's bounded product-path retry: live diagnosis confirmed ADR-0272 deliberately
   defines provision `ready` as domain start rather than serial-marker or SSH readiness, so a
   SUSE-only readiness drop-in and immediate raw-banner check cannot strengthen that contract.
   Fix the shared banner probe to close and reconnect when QEMU's host forward accepts before sshd
   answers, bounding each read inside its flow deadline; cover the accepted-but-late-banner
   sequence with a deterministic fake-clock test. Retain the viewer probe's 15-second bound but
   give the authorization preflight 30 seconds under ADR-0672, because the Leap live console
   showed sshd starting immediately after the shorter terminal window.
2. Add a SUSE-only, two-parameter live test that allocates, provisions, creates a Run, uploads and
   boots the v7.0 kernel, and force-crashes. Before upload, require the source tree's
   `kernelversion` to equal `7.0.0` and require the actual bzImage header release to equal
   `kernelrelease`; after boot, require the domain XML to name the per-Run staged kernel and carry
   a release-specific proof token. Before requesting capture, poll the named libvirt domain
   through `worker_libvirt_uri()` and require shutoff under a deadline below the existing
   120-second harvester fallback. Then poll `vmcore.fetch` to terminal failure. Assert the response
   is `readiness_failure`, carries
   `failure_detail_reason = "kdump_core_incomplete"`, and names the existing `host_dump` recovery.
   A generic failed job, no-core failure, successful core, or drain timeout fails the proof.
3. Preserve allocation release in `finally`; do not add a retry that could erase the first crash
   result.
4. Add focused tests for the direct domain-shutoff poll and any new job-failure assertion helper,
   including wrong domain state, deadline exhaustion, wrong category, missing reason, wrong
   remediation, canceled job, and timeout.
5. If the live capture returns a successful artifact despite the catalog's v7.0-incapable signal,
   inspect the guest's own status inputs before changing the provider contract. SUSE's final
   README records only process success; capture makedumpfile stderr and fail closed on its exact
   unsupported/incomplete warnings.
6. Bring up the host-process live stack from the exact checkout, onboard the demo project, set both
   image variables, and run the four SUSE parameters: two SSH and two incomplete-capture cases.
7. Confirm the deployed server/worker/reconciler source matches the tested commit. Record per-case
   result, duration, artifact digest, and commit without public machine identifiers.
8. Stop quest-created services and account for retained volumes/artifacts.
9. Commit the proof driver and runbook update as one conventional `test` or `docs`-paired commit,
   splitting only if repository hooks require independently valid changes.

Acceptance: each image independently proves baseline lifecycle and SSH; each boots the v7.0 test
kernel and reaches the exact existing incomplete-core remediation without publishing a vmcore.

Rollback: revert the test/runbook commit; live allocations are released by the test and services
are stopped through the runbook.

## Task 6: Update support documentation

**Files:** modify `docs/operating/platform-support.md` and
`docs/operating/runbooks/image-lifecycle.md`; update directly linked generated/index material only
when its repository guard requires it.

**Steps**

1. Add Tumbleweed and Leap 15.6 to the x86_64 rootfs matrix and name the two catalog identities.
2. State the verified package versions, both images' v7.0 kdump-incapable signal, Leap's absent
   drgn capability, and Tumbleweed's present drgn capability.
3. Document both build commands and the two live-proof environment variables. Distinguish catalog
   availability, successful image build, SSH proof, and incomplete-capture proof.
4. Run the repository's relevant documentation/link guards from the justfile.
5. Stage the exact Markdown paths, run `prek run`, re-add only rewritten staged paths, and commit
   with a conventional `docs` subject.

Acceptance: a reader can build and select either image without inferring unsupported kdump, drgn,
architecture, provider, or CI coverage.

Rollback: revert the documentation commit with the catalog change if the rows do not ship.

## Task 7: Review, verify, and deliver

1. Run `just test-changed`, the focused family/catalog/build-plane tests, `just lint`, and
   `just type` after implementation.
2. Run the quest's iterating adversarial review. Fix blocking findings in separate commits,
   disposition notes once, and rerun until approved or the review cap is reached.
3. Run the diff-scoped security pass. Fix in-scope findings and repeat the focused checks.
4. Run the simplification pass, preserving the frozen behavior and rerunning affected tests.
5. Re-read the complete diff against the frozen scope and approved design. Confirm public text is
   free of private host, user, network, serial, and location identifiers.
6. Run `just ci > /tmp/kdive-825-ci.log 2>&1 < /dev/null` as the final local pre-push parity gate
   and preserve its exit status.
7. Push the exact reviewed head, create the issue-linked pull request from a scanned body file, and
   wait for required checks. Do not merge without explicit authorization.

Acceptance: reviewed code and documentation match the approved surface, all focused/live evidence
is recorded, the full local gate and required PR checks are green, and the PR is ready for merge
handoff.
