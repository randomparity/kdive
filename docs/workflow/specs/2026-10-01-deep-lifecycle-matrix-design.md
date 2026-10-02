# Deep lifecycle across representative guests and pinned kernels — design

Issue: #2809 (epic #2803 entry 7). Governing records:
[ADR-0686](../../adr/0686-independent-coverage-obligations.md) (contract, qualifier) and
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) (evidence identity,
staged-image binding). No new ADR: every choice below stays inside those two records.

## Problem

The coverage contract requires `deep-lifecycle/<provider>/<arch>/<family>/<baseline>` cells with
the assertions `upload`, `install`, `boot-identity`, `modules`, `reconnect` and `cleanup`. No test
produces them. The existing spine (`tests/integration/test_live_stack.py::test_spine_over_the_wire`)
boots one hand-staged image with an environment kernel and checks neither the running build nor a
loaded module. The contract also gives #2809 the eleven lifecycle tool cells and every ppc64le
deep and tool cell, which the approved split moves to #3062 and #2818.

## Scope

1. **Ownership** (`scripts/coverage_campaign/`).
   - `obligations.toml`: the lifecycle tool group's `owner` becomes `3062`. Its `authority` flag
     is unchanged (#3062 owns re-checking it).
   - `contract.py` `_tool_cells`: a `3062` group cell on `remote-libvirt` goes to `2810` (as
     before); on ppc64le `local-libvirt` it goes to `2818`.
   - `contract.py` `_matrix_cells`: deep owner is `2810` for `remote-libvirt`, `2809` for
     `local-libvirt` x86_64, `2818` for `local-libvirt` ppc64le.
   - The deep scenario ID becomes `deep-lifecycle/<provider>/<baseline>`, so mapping the local node
     does not mark the remote cells implemented. Remote cells stay `pending-implementation` for
     #2810. The two local scenario IDs map to the new node; ppc64le local cells share it and report
     `missing-result` (owner #2818) until a native POWER host runs it, as image smoke does.
   - The deep cells keep the native roles (server, worker, reconciler): on the local-libvirt
     demo-up lane the scenario does not route through the provider authority. An installed
     authority is still judged (ADR-0715). #2810 and #2818 decide this for their lanes.
2. **Shared cell runner** (`tests/integration/live_stack/scenario.py`), extracted from
   `test_image_smoke_live.py` with no behaviour change to image smoke:
   `ScenarioStop`, `CellRun` (assertions, artifacts, observed `Context` fields), `run_cell`
   (identity → prerequisites → scenario → one record → pytest verdict), `on_catalog_system`
   (the local-libvirt frame: acquire a registered catalog image, allocate, provision to `ready`,
   observe the accelerator, run a body, prove `cleanup` with `release_and_verify`, record a cleanup
   attempt on failure), `authorize_ssh`, `ssh_probe`, `probe_new_boot`, `domain_xml`. Everything
   except `on_catalog_system` and `domain_xml` is provider-neutral; #2810 wraps the same deep body
   in its own remote frame.
3. **Deep inputs and probes** (`tests/integration/live_stack/deep_lifecycle.py`):
   - Representatives (one per family and architecture, Fedora and EL distinct):
     debian `debian-kdive-ready-13`; fedora `fedora-kdive-ready-44` (ppc64le
     `fedora-kdive-ready-44-ppc64le`); enterprise `rocky-kdive-ready-10` (ppc64le
     `rocky-kdive-ready-10-ppc64le`); suse `opensuse-tumbleweed-kdive-ready`. A unit test requires
     a representative of the right family and architecture for every deep cell of both arches.
   - Fixtures: `KDIVE_FIXTURE_ROOT/<baseline>` built and checked by `scripts/kernel_fixtures.py`
     (`verify`). Kernel inputs from the verified manifest: `kernel_sha256` = digest of the boot
     member the upload stages (x86_64 `bzImage`; ppc64le the `strip -s` copy of `vmlinux`),
     `kernel_source_sha` = pinned commit, `kernel_config_sha256` = `.config` digest,
     `compiler_id` = canonical identity of the manifest toolchain, `kernel_build_id` = vmlinux
     GNU build ID.
   - `gnu_build_id(notes)` parses the running kernel's `/sys/kernel/notes`.
   - Module probe: `modprobe loop` (`CONFIG_BLK_DEV_LOOP=m` in `fixtures/kernel/debug.config`),
     then `/sys/module/loop/initstate`, `modinfo -n`/`-F vermagic`, and the module file's SHA-256.
   - `deep_body(run, op, system_id, owned, *, project, entry, tree, manifest, tmp, staged_kernel)`: the
     provider-neutral assertion sequence of item 4; `staged_kernel(system_id) -> str` is the
     provider hook naming the installed kernel file (local: the domain XML `<kernel>`).
   - `bindings` command: the expected `Context` of every native local deep cell — host identity,
     catalog `guest_os`/arch, accelerator, staged image digest (as image smoke), kernel inputs from
     the verified fixture; null where unstaged or unbuilt.
4. **Test** `tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle`, `live_stack`,
   parametrized over the native local deep cells: `run_cell` → `on_catalog_system` with the
   representative → `deep_body`, which proves in order:
   - authorize a fresh key; SSH probe as root: uid 0, catalog OS/arch, baseline `boot_id`.
   - open an investigation; `runs.create` bound to the System.
   - **upload**: `build_and_upload_kernel(kernel_tree=<fixture>, evidence_dir=…, with_vmlinux=True,
     require_network=True, root_fs="ext4")`; the completed build's `build_id` equals the manifest.
     `with_vmlinux` is required: it sets the build's debuginfo reference, which is what makes
     install inject `lib/modules` into the guest.
   - **install**: `runs.install` drains, then (after boot) the domain XML `<kernel>` file's
     SHA-256 equals the bound `kernel_sha256`, and `runs.get` shows `install` and `boot` steps
     succeeded. The staged kernel path joins the owned files cleanup must prove absent.
   - `runs.boot` drains.
   - **reconnect**: `systems.ssh_info` again; the same key authenticates as root on a new
     `boot_id`.
   - **boot-identity**: `uname -r` equals the manifest release; the notes build ID equals the
     manifest build ID.
   - **modules**: `initstate` is `live`, `vermagic` starts with the release, and the module file's
     digest equals the copy `modules_install` staged for the upload.
   - The investigation is closed; **cleanup** is `release_and_verify` over the domain, its disks,
     the staged kernel and capacity.
   - Missing fixture root, invalid fixture, missing issuer/DB or unregistered image → `blocked`
     (`missing-prerequisite`). Non-candidate revision or dirty checkout → `failure` before any
     mutation. A failed assertion → `failure` with the assertions proven so far.
5. **Docs**: a deep-lifecycle subsection in `docs/operating/runbooks/live-testing.md`
   (fixture build, bindings, run, assemble, qualify).

## Failure model

1. Actors and deployments: an operator on a native x86_64 KVM lab host running the demo-up
   stack at the candidate SHA; later a native ppc64le host (#2818). CI never runs the live test;
   it runs the unit tests and `just coverage-check`.
2. Invariants and assets: a cell counts only with every assertion backed by an artifact and the
   deployed roles equal to the candidate; owned domains, disks, staged kernels and capacity are
   reclaimed; the shared lab host's other guests are untouched; published evidence holds no host
   names, paths or keys.
3. Accepted failure classes:
   - A killed pytest leaks its allocation until the lease expires (domain, disks, capacity); the
     runbook names the manual release. Whether teardown reclaims the staged kernel is unproven
     (install keeps it for the System's lifetime); if the first live cell shows it survives, that
     is a product defect filed before the full run, and `cleanup` fails on every cell until fixed.
   - A fixture kernel that cannot boot a representative's userspace (defconfig plus
     `debug.config`, no initrd) is a fixture defect: filed against the fixture owner, recorded as
     that cell's failure; the fixture is not changed here.
   - The qualifier trusts producer digests (ADR-0686 limit).
   - A representative stands for its family; other images of the family are image smoke's.
4. Covered elsewhere: remote deep cells #2810; ppc64le execution #2818; lifecycle tool cells
   #3062; RPM-host fixture provenance #3063; scheduling/release gate #2819.

## Threat note

Guest SSH output is untrusted data. The module path it returns is only used to name a file under
the upload's own `modstage` directory; a path that resolves outside it fails the assertion.
Commands sent to the guest are literals.

## Success

- `just coverage-check` passes; the lifecycle tool cells are owned by #3062 (x86_64 local),
  #2818 (ppc64le local), #2810 (remote); deep cells by #2809 (x86_64 local), #2818, #2810.
- On the lab host at the deployed candidate, each of the eight x86_64 cells writes one record and
  `qualify` reports its outcome; the PR and runbook record those per-cell outcomes (sanitized),
  with a linked defect issue for every failing cell.
- Image smoke behaves as before on the shared runner.
