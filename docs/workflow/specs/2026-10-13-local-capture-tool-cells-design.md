# Local crash capture and introspection HTTP evidence

## Problem and authority

Issue #2814 and epic #2803 require proof that the capture and introspection
tools return useful data from the real crashed or running kernel, with correct
build identity, through the public HTTP surface. Current main has the
over-the-wire spine (`tests/integration/test_live_stack.py`) but no bound tool
cells for `control.force_crash`, `vmcore.fetch`, `introspect.from_vmcore`,
`introspect.run`, `introspect.script` or `postmortem.crash`. Coverage group 2814
has 684 generated cells, all unbound and all owned by #2814, each requiring the
`authority` role.

The frozen charter is the issue's `WORK:SCOPE` with token `q2814-35627d21`
(campaign 73d4edacff-8cf13f64-8877-4610-beaf-496193501043), which reuses the
operator's 2026-10-06/07 approval of the exclusions unchanged. Operator decision
R2 (2026-10-13) landed in PR #3211: a capability cell follows the provider's
support verdict (ADR-0722 amendment 2026-10-13), with a `capability` harness
boundary. Operator decision D1-A (2026-10-13) removes the group's `authority`
flag. ADR-0757 made `vmcore.fetch` refuse `fadump` on an x86_64 System and added
the System-specific `systems.get.data.supported_capture_methods` list. No
product source, public contract or ADR changes here.

## Contract changes

- Group 2814 in `scripts/coverage_campaign/obligations.toml` drops
  `authority = true` and keeps `kernel = true` (D1-A). The six tools need the
  server, worker and reconciler; nothing in their paths reads the authority
  service.
- `_tool_cells` routes the group's ppc64le cells to #2818 and its
  remote-libvirt cells to #3114, beside the existing #2815/#3062/#3112/#3119
  routing. The 164 x86_64 local-libvirt cells stay owner 2814.
- Those 164 cells: 28 `functional` (six tools; `vmcore.fetch` as `kdump` and
  `host_dump`; four configuration × exposure each), four `unsupported`
  (`vmcore.fetch/fadump`, `ArchTraits.fadump_capture`) and 132 `rejection`,
  four of them `capability` (`vmcore.fetch/fadump`). Each is bound in
  `[implementations]` to the new carrier's one test function.
- A local scenario id names no architecture, so the 160 local ppc64le cells
  (owner #2818) share that node, as #2815's do. The carrier runs only cells of
  its host's architecture; on a ppc64le host it stops each as `blocked`
  ("#2818's lane"), and #2818 rebinds them.

The matrix digest moves; earlier qualifications against it fail
`matrix-mismatch`, which is the intended behaviour.

## Approach and alternatives

Chosen: one scenario module, `tests/integration/live_stack/capture_cells.py`,
and one carrier, `tests/integration/test_capture_tool_cells_live.py`, on the
merged tool-cell frame, following #2815's `power_snapshots.py`. The functional
frame is `on_lane_system` with a `provision` wrapper that adds the crash
options the tool needs to the lane profile, then `deep_body` boots the verified
`longterm` fixture kernel.

Rejected:

- Extend `test_live_stack.py`'s spine. It is one shared-project flow with a
  stack-wide token and no per-cell records; it cannot record per-cell evidence.
- Share one crashed System between cells. Each record would depend on another
  cell's order and failure, and `vmcore.fetch` admits one method per Run.
- Capture inside the catalog image's own kernel. Its identity is not a pinned
  fixture, so the cells' kernel inputs could not be bound.

## Functional preparation

Each functional cell owns one System in a fresh project:

1. The frame provisions the lane image (`fedora-kdive-ready-44`) with the
   tool's crash options: `kdump` adds `crashkernel` (the x86_64 trait default)
   and `destructive_ops = ["force_crash"]`; `host_dump` adds
   `destructive_ops` and `debug.preserve_on_crash = true` (no `crashkernel`,
   so the domain stays active for the memory dump); `live` adds nothing;
   `target` adds only `destructive_ops`.
2. `deep_body` uploads, installs and boots the fixture kernel and proves its
   build identity and a loaded `loop` module. It now returns the Run id it
   created; its investigation is closed on return, which no capture or
   introspection path reads.
3. A sentinel process is started from a copy of `sleep` named
   `kcov<8 hex>`, so its command name is a known, unique workload fact.
4. For `kdump`, the cell waits until `/sys/kernel/kexec_crash_loaded` reads
   `1` (the guest's kdump service rebuilds its initramfs for the new kernel)
   before crashing, with a 600 s budget; a timeout fails the cell. A guest
   whose `kdump.service` has failed cannot build its kdump initramfs for the
   booted kernel, so the wait stops the cell as `blocked` (missing
   prerequisite) at once (operator decision, 2026-10-14).
5. After a crash the cell drains the `force_crash` job to `succeeded` and waits
   for `systems.get` to read `crashed`.

The MCP server process runs `postmortem.crash` and `introspect.from_vmcore`, so
it needs crash(8) and drgn; the worker needs the libguestfs Python binding (the
kdump harvest); the guest image carries drgn for the live tools. A tool answering `missing_dependency` stops the cell as
`blocked` with that reason, never as a pass or a product failure.

Only the tool under test goes through the cell's exposure and token; supporting
calls use the frame's operator client, or an admin client of the same project
for `control.force_crash`.

| Tool (variant) | Options | Exposure call and observed effect |
|---|---|---|
| `control.force_crash` | kdump | Admin token. The job drains `succeeded`; `systems.get` reads `crashed`; a console part sealed after the call contains `Kernel panic - not syncing` and the fixture release. |
| `vmcore.fetch` (`kdump`, `host_dump`) | as named | After the operator crash, `vmcore.fetch(method)` drains `succeeded`; job, Run and artifact refs agree and expose no raw core; two `artifacts.fetch_raw` downloads give identical SHA-256 and the advertised size; the core's VMCOREINFO (the ELF `VMCOREINFO` note for `host_dump`, the kdump-compressed sub-header block for `kdump`) carries exactly one `BUILD-ID=` equal to the fixture build ID and one `OSRELEASE=` equal to the fixture release. Strings elsewhere in guest memory are not read. |
| `introspect.from_vmcore` | kdump | After crash and kdump capture: `report.sysinfo.release` and `machine` equal the fixture release and `x86_64`, `nodename` equals the guest's, and `loop`, the module the fixture boot loaded as the controlled workload, is listed in `report.modules`. Success implies the server's build-ID provenance check passed. |
| `postmortem.crash` | kdump | After crash and capture, `commands=["sys", "ps", "mod"]`: the transcript names the fixture release, the sentinel's command name and `loop`. |
| `introspect.run` | live | After `debug.start_session(transport="drgn-live")` with no `missing_debuginfo`, helpers `sysinfo` and `modules` match the guest's release, nodename and `loop`; the session ends `detached`. |
| `introspect.script` | live | Same session; a fixed drgn script prints the release and every thread whose `comm` starts `kcov`; output names the fixture release and exactly the sentinel's pid and name, `truncated` false; the session ends `detached`. |
| `vmcore.fetch` (`fadump`, unsupported) | live | After the fixture boot, `systems.get` on the System lists `supported_capture_methods` containing `kdump` and `host_dump` but not `fadump` (ADR-0757). An absent field fails the cell. |

Cleanup is the frame's existing proof: allocation released, System torn down,
domain and owned disks absent, capacity restored. A crashed System releases
through the same path. Captured cores and redacted logs are the tool's
product: Run-owned `vmcore` artifacts, which no reclaim path removes today
(artifact retention sweeps only build artifacts). They are retained evidence,
not owned resources of the cell. The record names their artifact ids, and the
runbook states a run's object-store footprint.

## Rejection preparation

`capture_target` prepares one target per stack and caches it like
`lane_target`: a lane System booted with the fixture kernel (`target`
options), its Run, and a drgn-live session opened then ended (`detached`), after
which the frame tears the System down. Every role and project check of the six
tools runs before any state check, so the torn-down System, its Run and the
detached session are valid targets. The target carries the `force_crash`
opt-in, so on it only the admin role can deny `force_crash`. A failed
preparation is remembered and replayed for every later cell.

A rejection or capability record carries exactly the lane context (host,
`guest_os`, `guest_arch`, accelerator, `image_sha256`) and no kernel field:
those cells declare no kernel inputs, so their bindings hold none. The target
boots the fixture on a scratch `CellRun` and caches only the lane keys.

| Boundary | Call and grants | Expected |
|---|---|---|
| authentication | the tool's arguments; a viewer token re-signed by an untrusted key | HTTP 401; the issued token's call is not 401 |
| authorization | the tool's gate role minus one: operator for admin, viewer for contributor, member with no role for viewer | `authorization_denied` |
| project-isolation | an admin of another project (so the tool is visible to it), with an absent-id twin | `configuration_error` (`force_crash`, `introspect.run`, `introspect.script`) or `not_found`, identical to the twin's masked answer |
| validation | the id argument given as an integer | the frame's validation rule |

Protected state is the target project's snapshot, the jobs naming the target's
System or Run, and whether its domain is defined.

The `capability` cell (`vmcore.fetch/fadump`) needs a crashed, Run-bound System
(ADR-0722, 2026-10-13). Its frame is the lane's System frame
(`on_catalog_system`) with `host_dump` options and no fixture kernel; the body
observes the guest (`observe_guest`, which records the lane context), opens an
investigation, calls `runs.create(investigation_id, system_id, build_profile)`,
crashes the System through the admin client as in step 5 above, and waits for
the project snapshot to settle (`tool_cells.settled`). It then calls
`prove_rejection(run, caller, "capability", ...)`, closes the investigation, and
the frame proves cleanup. A contributor's `vmcore.fetch(method="fadump")` must be refused
`configuration_error` with `data.reason = capability_unsupported`; the jobs and
artifacts naming the Run and the System's state are unchanged across the call,
and the frame proves cleanup.

## Failure model

1. Actors and deployments:
   - A maintainer running the carrier on a KVM x86_64 host against a stack
     brought up by `scripts/demo-up.sh`, in one configuration per run.
   - CI runs only the unit tests and the contract test.
2. Invariants and assets at stake:
   - Evidence honesty: a record passes only on the observed effect and the
     deployed candidate's identity.
   - Host resources: every domain, disk and allocation a cell creates is
     released, including after a failure.
   - The coverage contract shared with #2813, #3114, #2818.
3. Accepted failure classes:
   - A slow kdump initramfs rebuild in the guest fails the cell after its
     budget; bounded and visible.
   - A host_dump core is about as large as guest RAM (2 GiB); two downloads
     cost time, accepted for the immutability proof.
   - Captured cores stay in the object store with no reclaim path: about
     8 GiB of `host_dump` cores per configuration (four 2 GiB cores) and a
     few hundred MiB of kdump cores, stated in the runbook. The missing
     `vmcore` reclaim is a product gap returned as a follow-up candidate.
   - A server missing crash(8) or drgn, or a worker missing the guestfs
     binding, records `blocked`, not a pass.
4. Covered elsewhere:
   - Remote capture cells: #3114. ppc64le cells and native fadump: #2818.
   - POWER compressed core: #2763. Debug tools: #2813.
   - Kernel and image fixture production: #3101.

## Threat model

The change adds test code only. It reads guest output (probe text, console and
core bytes) produced by a guest the test provisioned; parsing is bounded to
named fields and byte patterns, and evidence records carry digests, booleans
and counts, never core bytes or console text. Tokens are minted for synthetic
projects. Out of scope: hostile guests, which the deployment does not run.

## Verification and completion

- Unit tests (`tests/integration/live_stack/test_capture_cells.py`): the
  provision options per tool; the VMCOREINFO reader on synthetic ELF and
  kdump-compressed cores, including a stray `OSRELEASE=%s` outside the note;
  the console panic match; the transcript checks; the rejection arguments,
  categories and grants per tool and boundary; and the cached target keeping
  only lane keys.
- Contract tests (`tests/scripts/test_coverage_contract.py`): group 2814 roles
  carry no `authority`; its ppc64le cells are #2818's, its remote cells
  #3114's and unbound, its 164 x86_64 local cells #2814's; all 324 local cells
  name the carrier node.
- Live: both configurations, each exposure, on a lab x86_64 host with the
  deployed head equal to the committed candidate; results, including blocked or
  failing cells, recorded in `docs/operating/runbooks/live-testing.md`.
