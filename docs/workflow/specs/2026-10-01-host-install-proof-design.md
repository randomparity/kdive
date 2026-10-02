# Host-installation proof — design (#2807)

Decision: [ADR-0716](../../adr/0716-host-install-evidence-producer.md). Contract:
[ADR-0686](../../adr/0686-independent-coverage-obligations.md),
[coverage qualification](../../development/coverage-qualification.md).

## Problem

The documented local-libvirt host path has never been proven from a clean baseline through a
real guest, then through a repeat of setup. Success on a warmed machine says nothing about that
contract. The coverage contract already requires one `host-install/local-libvirt/x86_64/<family>`
cell each for `debian`, `fedora` and `enterprise`. Each cell needs six assertions. No producer
exists, and `[implementations]` binds no node.

## Goals

1. A reusable, host-agnostic producer that turns one exclusive clean host into one version-1
   result for its family cell. Its x86_64 path is proven here. Its ppc64le path (a native
   `bundle` on the POWER build host, `kvm-hv` binding, ELF boot member) is covered by focused
   tests here and proven live by #2818.
2. Live clean and repeat runs on one x86_64 representative per family: Ubuntu 26.04, Fedora 44
   and Rocky 10. Each cell is recorded honestly. A failed or blocked cell links a defect issue.
3. A committed, sanitized proof record, plus operator and qualification docs that match what ran.

Out of scope (operator-approved): native POWER host preparation (#2818); catalog smoke (#2808);
deep lifecycle (#2809); the remote provider (#2810); failure and resource-limit scenarios
(#2816); a lab reset target (lab repository); new host families, including SUSE (#2803
non-goal); release gating (#2819); and fixing product defects (separate linked issues).

## Design

### Runner — `scripts/host_install_proof.py`

The runner has three subcommands. `bundle` cuts the kernel bundle where the fixture lives, `run`
proves one host, and `merge` combines runs.

`bundle --fixture DIR [--baseline longterm] --output BUNDLE` must run on a host where
`kernel_fixtures.verify` accepts the fixture in place. That is the fixture's native build host,
because the fixture Makefile records its own path; for ppc64le it is the POWER build host. It
uses the documented recipe: `make modules_install INSTALL_MOD_STRIP=1` into scratch space, a
native `strip` of the ppc64le boot member, then a tar that lists the boot member first as
`boot/vmlinuz` followed by `lib/modules`, excluding `build` and `source`. The resulting
directory holds `kernel.tar.gz`, `effective_config` (the fixture `.config`) and
`manifest.json`. The cut duplicates `combined_kernel_tar` in the live-stack spine, because
`scripts/` does not import test modules.

`uv run python -m scripts.host_install_proof run` takes these inputs:

- `--target USER@HOST` and `--known-hosts FILE`. Every `ssh` and `scp` call shares one option
  list: `BatchMode=yes`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=FILE`,
  `ConnectTimeout=30`, `ServerAliveInterval=30` and `ServerAliveCountMax=4`.
- `--family {debian,fedora,enterprise}`.
- `--candidate SHA`. It must equal the controller checkout's clean `HEAD`, because the matrix is
  built from that checkout.
- `--bundle BUNDLE`, from `bundle`. `run` checks that every manifest field it binds is present,
  that the `effective_config` digest equals `artifacts[".config"]`, and that the manifest
  architecture equals the guest image row's.
- `--guest-image NAME`, an x86_64 or ppc64le catalog row.
- An optional `--operator-prerequisites FILE`.
- `--output DIR`, which must be new.

It runs these steps in order. Every step is a separate `ssh` invocation, and therefore a new
login session. A step is a fixed script sent as the remote command `bash -lc <quoted script>`,
with ssh's stdin at `/dev/null`, so no command can consume the script. Its only interpolated
values are validated and shell-quoted. Each step has a wall-clock limit (`STEP_TIMEOUT_S`, 3 h
for the setup and image steps and 1 h for the rest). A step that hits its limit is a failed
step whose exit code is recorded as `timeout`.

| # | Step | What runs on the target |
|---|---|---|
| 1 | `observe-host` | Read `/etc/os-release` (`ID:VERSION_ID`), `uname -m`, the confinement mode, `/dev/kvm`, and `sudo -n true`. Confinement is `getenforce` for SELinux, or `/sys/module/apparmor/parameters/enabled` for AppArmor. Check the clean markers: no `~/kdive`, `/opt/kdive-live-worker-lifecycle`, `/var/lib/kdive` or `kdive-live-worker*` units. The runner then stops with exit 2, before any mutation, when the observed distribution's catalog family (`image_family`) differs from `--family` or the architecture differs from the bundle's. |
| 2 | `operator-prerequisites` | The supplied file, if given. This covers the documented operator duties: a Docker engine and access where the distribution has no known package, the EL CRB and source repositories, and POWER Rust. |
| 3 | `bootstrap` | Git through the distribution package manager when absent, then the documented `uv` installer. |
| 3a | `just` | `uv tool install rust-just`, in a new login, so the `~/.local/bin` the installer created is on `PATH`. |
| 4 | `clone` | Clone a `git bundle` of the candidate (copied by `scp`) to `~/kdive`, detached at the candidate, with `origin` set to the public URL and `origin/main` fetched, as a documented clone has it (the setup hooks read it). |
| 4a | `kernel-source` | `scripts/fetch-kernel-tree.sh ~/src/linux` at the bundle's source commit. The example README lists a kernel tree at `KDIVE_KERNEL_SRC` as a prerequisite, and host preparation grants workers traversal only to a tree that already exists. |
| 5 | `setup` | `just setup` |
| 6 | `prepare` | `just prepare-local-libvirt-host`, with the documented local witness DSN exported and an empty line on stdin for the become prompt. |
| 7 | `preflight` | `just check-local-libvirt` |
| 8 | `stack` | `examples/local-libvirt/demo-up.sh`, with `KDIVE_DEMO_WORKSPACE=~/kdive-demo` |
| 9 | `guest-image` | `examples/local-libvirt/build-image.sh <guest-image>`, then `sha256sum` of the published image, which becomes the binding's `image_sha256`. |
| 10 | `first-boot` | Copy the kernel bundle, then run the node with `HOST_INSTALL_PHASE=first-boot` and `HOST_INSTALL_OUTPUT` set to a new per-phase directory that must not already exist. |
| 11–14 | `repeat-*` | Steps 5–8 again. |
| 15 | `second-boot` | The node again, with `HOST_INSTALL_PHASE=second-boot`. |

Each step's transcript, exit code and duration go to a private `steps/` directory.
`summary.json` lists each step's `name`, `exit_code` and `seconds`, plus the `outcome`. Phase
records are fetched with `scp`.

After step 1 identifies the host, the runner writes the binding (`binding.json`) for that cell
from three sources. The host platform comes from step 1. The guest OS and architecture come from the
catalog row. The accelerator is `kvm` on x86_64 and `kvm-hv` on ppc64le. The kernel inputs come
from the checked bundle manifest:

- `kernel_source_sha` is the manifest `source.commit`.
- `kernel_config_sha256` is the manifest `artifacts[".config"]`.
- `compiler_id` is `kernel_fixtures.identity(manifest["toolchain"])`.
- `kernel_build_id` is the manifest `build_id`.
- `kernel_sha256` is the SHA-256 of the bundle's `boot/vmlinuz` member.

`image_sha256` is added after step 9 and before step 10.

`compose(...)` then writes `result.json`, a one-element `Evidence` array:

- `node_id` and `scenario_id` come from `build_contract()` for the cell, and
  `input_sha256 = digest(binding)`.
- `deployed_roles` contains each role that both phases reported as the same full SHA.
- `context` is the first phase's observed context. It is the binding only when no phase record
  exists. Any other phase context stays visible in its own phase artifact.
- A phase record whose `phase` differs from its slot counts as absent.
- Each of the six assertions maps to the SHA-256 of a canonical JSON artifact stored as
  `artifacts/<sha256>.json`.
- `duration_seconds` is the wall time of the whole run.

| Assertion | Artifact content | Holds when |
|---|---|---|
| `clean-install` | Steps 1–9: exit codes, durations, transcript digests, clean markers, operator-file digest | Every listed step exits 0, and step 1 found all markers absent. |
| `first-boot` | The first phase record | The phase booted the bundle's kernel (console release observed). |
| `repeat-setup` | Steps 11–14 | Every listed step exits 0. |
| `second-boot` | The second phase record | The phase booted the bundle's kernel. |
| `confinement` | Host mode at step 1 and in each phase, the guest process labels, and each phase's installed-prerequisite observations (worker interpreter imports, lifecycle socket, operator group, published libvirt endpoint, lifecycle-witness revision) | The host enforces in all three samples (`Enforcing`, or AppArmor enabled `Y`), each phase observed a confined qemu process, and each phase's installed-prerequisite checks hold. |
| `cleanup` | Cleanup observations from both phases | Each phase saw its system `torn_down` and its domain absent. |

The outcome is decided in this order:

1. No result at all when step 1 cannot identify the host: ssh exits 255, or `/etc/os-release` or
   `uname -m` falls outside the evidence schema. The runner writes `summary.json` and exits 3,
   and the cell qualifies `not-run`. The proof record reports it as blocked.
2. `blocked`, with `impediments: ["missing-prerequisite"]` and the binding as context, when an
   identified host is not clean, lacks non-interactive sudo, or lacks `/dev/kvm`.
3. Otherwise `failure` if any assertion does not hold, a role is missing or not the candidate,
   or the phase contexts differ from each other or from the binding.
4. Otherwise `success`.

A failed or timed-out install or setup step stops the run, and the result still lists only
what ran. A failed boot phase that wrote its record continues through repeat setup and the
second boot, so both phases are recorded. `run`
exits 0 on `success`, 1 on `failure`, 3 on `blocked` or an unidentified host, and 2 on invalid
input. `summary.json` carries a `reason` (`unidentified`, `not-clean`, `no-sudo`, `no-kvm`,
`host-mismatch`); only `not-clean` and `unidentified` can clear after a reset.

`merge --output DIR RUN...` combines run directories into `inputs.json` and `results.json` for
`coverage_campaign qualify`. Two runs that bind the same cell, or carry different candidates or
matrices, are an error (exit 2).

### Node — `tests/integration/test_host_install_live.py`

`test_installed_host_boots_pinned_kernel` is marked `live_stack`. It skips unless
`HOST_INSTALL_PHASE`, `HOST_INSTALL_OUTPUT`, `HOST_INSTALL_BUNDLE` and `HOST_INSTALL_CANDIDATE`
are set. It runs from `~/kdive`, with `examples/local-libvirt/env.sh` sourced, and writes
`<output>/phase.json`. It makes these observations:

- **Deployed revisions.** All four contract roles come from the shared ADR-0715 reader
  (`tests/integration/live_stack/evidence.py` `run_identity`). `server` and `reconciler` come
  from their aux `/readyz`, `worker` from every running fixed slot, and `authority` from the
  provider authority's installed revision. A role it cannot read stays unknown. The checkout
  must be `HEAD == candidate` with no modified source.
- **Installed prerequisites.** The lifecycle venv imports `guestfs`, `libvirt` and `kdive` under
  `-I`. `systemctl is-active kdive-live-worker-lifecycle.socket` reports active. The operator
  belongs to `kdive-live-control`. `KDIVE_LIBVIRT_URI` names the published session endpoint.
  The lifecycle witness's `/opt/kdive-live-worker-lifecycle/revision` stamp equals the
  candidate, and the digest of its venv's `kdive` package `.py` files equals the checkout's
  `src/kdive`, since the stamp alone can lie. This is a prerequisite check, not a contract role.
- **Boot.** The node runs `allocations.request` and `systems.provision` with the local rootfs and
  `boot_method: direct-kernel`, and waits for `ready`. It then runs `investigations.open` and
  `runs.create`, uploads `kernel` and `effective_config`, and runs `runs.complete_build`,
  `runs.install` and `runs.boot`. The boot passes only if the run's console artifact contains
  `Linux version <manifest release>`.
- **Confinement.** While the guest runs, the node reads the security label of the qemu process
  whose command line names the system's domain (`ps -eo label,args`). The label must contain
  `svirt_t` under SELinux. Under AppArmor it must be the per-domain `libvirt-<domain uuid>`
  profile in enforce mode, matched against the UUID in the domain XML. The host mode is
  re-read as well. If session-mode qemu turns out not to be confined this way, the cell fails
  and a defect is linked; the rule is not loosened.
- **Cleanup.** The node runs `allocations.release` and waits for `systems.get` to report
  `torn_down`. `virsh -c $KDIVE_LIBVIRT_URI list --all --name` must not contain the domain.
  A `finally` block releases the allocation on any failure path.
- **Context.** Each field is observed independently:

  | Field | Observed from |
  |---|---|
  | `host_os` | `/etc/os-release` |
  | `host_arch` | `uname` |
  | `guest_os`, `guest_arch` | The catalog row for the published image name |
  | `accelerator` | Domain XML `type='kvm'`, mapped by the host architecture |
  | `image_sha256` | The image file |
  | `kernel_sha256` | The uploaded tar member |
  | Remaining kernel fields | The bundle manifest, after its `.config` digest matches `effective_config` |

`phase.json` records `passed`, `booted`, `phase`, `deployed`, `context`, and per-check observations and
failures. The node asserts `passed`. A missing `phase.json` counts as a failed phase.

### Wiring

- `obligations.toml` gets one additive line in `[implementations]`, mapping `"host-install"` to
  the node. Implementations bind by scenario, so all six host-install cells gain the node: the
  three x86_64 cells here, and the three ppc64le cells #2818 owns, which move from
  `pending-implementation` to `missing-result`. The existing contract test's "no cell is bound"
  assertion narrows to the cells outside this scenario.
- The docs gain the runner usage and output contract (coverage qualification) and the proven
  family status (local-libvirt). The install page gains one bounded sentence: on a host whose
  sudo policy needs no password, as on the proof hosts, the recipe's become prompt accepts an
  empty line. The interactive default stays as documented.
- The proof record is `docs/design/2026-10-01-host-install-proof-record-2807.md`.

## Failure model

1. **Actors and deployments.** A trusted operator on a controller, with SSH and non-interactive
   sudo on exclusive disposable lab hosts that are reset to clean before and after each run. No
   CI job, tenant or shared host is involved.
2. **Invariants and assets.**
   - Evidence identity: a cell reaches `success` only through a full clean run at the candidate.
   - Lab privacy: no host name, address or account appears in committed evidence or docs.
   - The target host is root-mutated by design, and only exclusive targets may be used.
3. **Accepted failure classes.**
   - A non-exclusive target is accepted only to the degree the clean-marker check detects it.
     A pre-existing kdive state is blocked, and the operator must reset the host first.
   - A fabricated producer digest is outside the qualifier's scope (ADR-0686 "Limits").
   - Guest-image digests are host-built and not reproducible across hosts. They are bound per
     run and are not compared across families.
4. **Covered elsewhere.**
   - The host reset is owned by the lab repository.
   - The canonical `qualify` checks are owned by ADR-0686.
   - Scheduling and release enforcement are owned by #2819.
   - POWER runs are owned by #2818.

### Threat model

- **Boundaries.** The runner adds an SSH command channel to a privileged remote host. The host's
  `phase.json` and transcripts are read back into the controller as data. Nothing is widened in
  the product.
- **Actors.** The operator is trusted. The target host is trusted only for its own observations.
  A compromised host can lie in `phase.json`, which the accepted class above covers.
- **Controls.**
  - The target, the image name and the paths are validated against fixed patterns and
    `shlex.quote`d. Scripts are fixed text.
  - Host keys are pinned and strict.
  - `phase.json` is parsed with size bounds into typed fields. Unknown or missing fields fail.
  - The witness DSN is the documented disposable local value and never reaches the evidence.
  - Transcripts stay in the private output directory. Only the digests enter `result.json`.
- **Out of scope.** The trust of the bootstrap download (the upstream `uv` installer is the
  documented path), and the hardening of the lab network.

## Success

- A unit suite proves the runner's pure parts. They compose an evidence record that `qualify`
  accepts for a synthetic all-pass run. They yield `failure` for a failed step, a missing phase,
  a role mismatch, a context mismatch or a phase record in the wrong slot. They yield `blocked`
  for a non-clean host. The ssh and scp argv carry the shared options, stdin is `/dev/null`,
  step scripts contain only quoted values, and both architectures get the right accelerator and
  boot member.
- `coverage-check` passes with the node bound.
- Each of the three families has a run directory whose cell verdict under `qualify` is
  `success`, or `failure`/`blocked` with a linked issue. The proof record holds the per-cell
  outcomes, step durations and the sanitized identities. It also quotes each family's operator
  prerequisite file verbatim (sanitized) next to its digest. Each line of that file cites the
  install or local-libvirt passage that makes it an operator duty. A line with no such citation
  is an undeclared prerequisite: the cell is recorded as `failure` with a linked issue.

## Validation

| Contract | Mode |
|---|---|
| Evidence composition and outcome rules | `focused-test` (`tests/scripts/test_host_install_proof.py`) |
| Step script quoting and target/name validation | `focused-test` (same file) |
| Phase-record parsing bounds | `focused-test` (same file) |
| `merge` collision rules | `focused-test` (same file) |
| Node pure helpers: console release match, qemu label classification, package digest | `focused-test` (`tests/scripts/test_host_install_proof.py`, as the helpers live in the runner module) |
| Manifest binding to the node | `focused-test` (`tests/scripts/test_coverage_contract.py` plus `just coverage-check`) |
| Live node behaviour | `task-test-not-applicable`: it needs a freshly installed KVM host. It is proven by the live runs in the proof record. |
| Docs | `task-test-not-applicable`: prose, gated by `just docs-check` and `docs-links` |
