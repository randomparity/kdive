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
   result for its family cell. It must also run unchanged on native ppc64le hosts for #2818.
2. Live clean and repeat runs on one x86_64 representative per family: Ubuntu 26.04, Fedora 44
   and Rocky 10. Each cell is recorded honestly. A failed or blocked cell links a defect issue.
3. A committed, sanitized proof record, plus operator and qualification docs that match what ran.

Out of scope (operator-approved): native POWER host preparation (#2818); catalog smoke (#2808);
deep lifecycle (#2809); the remote provider (#2810); failure and resource-limit scenarios
(#2816); a lab reset target (lab repository); new host families, including SUSE (#2803
non-goal); release gating (#2819); and fixing product defects (separate linked issues).

## Design

### Runner — `scripts/host_install_proof.py`

`uv run python -m scripts.host_install_proof run` takes these inputs:

- `--target USER@HOST` and `--known-hosts FILE`. Strict host-key checking is used and
  `BatchMode` is on.
- `--family {debian,fedora,enterprise}`.
- `--candidate SHA`. It must equal the controller checkout's clean `HEAD`, because the matrix is
  built from that checkout.
- `--fixture DIR`, a pinned kernel fixture checked by `kernel_fixtures.verify`.
- `--baseline`, which defaults to `longterm`.
- `--guest-image NAME`, an x86_64 or ppc64le catalog row.
- An optional `--operator-prerequisites FILE`.
- `--output DIR`, which must be new.

It runs these steps in order. Every step is a separate `ssh` invocation, and therefore a new
login session. A step is a fixed `bash -l` script whose only interpolated values are
shell-quoted.

| # | Step | What runs on the target |
|---|---|---|
| 1 | `observe-host` | Read `/etc/os-release` (`ID:VERSION_ID`), `uname -m`, SELinux or AppArmor mode, `/dev/kvm`, and `sudo -n true`. Check the clean markers: no `~/kdive`, `/opt/kdive-live-worker-lifecycle`, `/var/lib/kdive` or `kdive-live-worker*` units. |
| 2 | `operator-prerequisites` | The supplied file, if given. This covers the documented operator duties: a Docker engine and access where the distribution has no known package, the EL CRB and source repositories, and POWER Rust. |
| 3 | `bootstrap` | Git through the distribution package manager when absent, the documented `uv` installer, then `uv tool install rust-just`. |
| 4 | `clone` | Clone a `git bundle` of the candidate (copied by `scp`) to `~/kdive`, detached at the candidate, with `origin` set to the public URL. |
| 5 | `setup` | `just setup` |
| 6 | `prepare` | `just prepare-local-libvirt-host`, with the documented local witness DSN exported and an empty line on stdin for the become prompt. |
| 7 | `preflight` | `just check-local-libvirt` |
| 8 | `stack` | `examples/local-libvirt/demo-up.sh`, with `KDIVE_DEMO_WORKSPACE=~/kdive-demo` |
| 9 | `guest-image` | `examples/local-libvirt/build-image.sh <guest-image>`, then `sha256sum` of the published image, which becomes the binding's `image_sha256`. |
| 10 | `first-boot` | Copy the kernel bundle, then run the node with `HOST_INSTALL_PHASE=first-boot`. |
| 11–14 | `repeat-*` | Steps 5–8 again. |
| 15 | `second-boot` | The node again, with `HOST_INSTALL_PHASE=second-boot`. |

Each step's transcript, exit code and duration go to a private `steps/` directory. Phase records
are fetched with `scp`.

After step 1 succeeds, the runner writes the binding (`binding.json`) for that cell from three
sources. The host platform comes from step 1. The guest OS and architecture come from the
catalog row. The accelerator is `kvm` on x86_64 and `kvm-hv` on ppc64le. The kernel inputs come
from the verified fixture:

- `kernel_source_sha` is the manifest `source.commit`.
- `kernel_config_sha256` is the manifest `artifacts[".config"]`.
- `compiler_id` is `kernel_fixtures.identity(manifest["toolchain"])`.
- `kernel_build_id` is the manifest `build_id`.
- `kernel_sha256` is the SHA-256 of the bundle's `boot/vmlinuz` member.

`image_sha256` is added after step 9 and before step 10.

The runner cuts the bundle on the controller with the documented recipe: `make modules_install
INSTALL_MOD_STRIP=1` into scratch space, then a tar that lists the boot member first as
`boot/vmlinuz`. On ppc64le a stripped copy of the boot member is used. The bundle directory holds
`kernel.tar.gz`, `effective_config` and `manifest.json`.

`compose(...)` then writes `result.json`, a one-element `Evidence` array:

- `node_id` and `scenario_id` come from `build_contract()` for the cell, and
  `input_sha256 = digest(binding)`.
- `deployed_roles` contains each role that both phases reported as the same full SHA.
- `context` is the phase-reported context when both phases agree, and the binding otherwise.
- Each of the six assertions maps to the SHA-256 of a canonical JSON artifact stored as
  `artifacts/<sha256>.json`.
- `duration_seconds` is the wall time of the whole run.

| Assertion | Artifact content | Holds when |
|---|---|---|
| `clean-install` | Steps 1–9: exit codes, durations, transcript digests, clean markers, operator-file digest | Every listed step exits 0, and step 1 found all markers absent. |
| `first-boot` | The first phase record | The phase passed. |
| `repeat-setup` | Steps 11–14 | Every listed step exits 0. |
| `second-boot` | The second phase record | The phase passed. |
| `confinement` | Host mode at step 1 and in each phase, plus the guest process labels | The host enforces in all three samples, and each phase observed a confined qemu process. |
| `cleanup` | Cleanup observations from both phases | Each phase saw its system `torn_down` and its domain absent. |

The outcome is decided in this order:

1. `blocked`, with `impediments: ["missing-prerequisite"]`, when the target is unreachable (ssh
   exit 255), step 1 finds a non-clean host, non-interactive sudo is missing, or `/dev/kvm` is
   missing.
2. Otherwise `failure` if any assertion does not hold, a role is missing or not the candidate,
   or the phase contexts differ from each other or from the binding.
3. Otherwise `success`.

A failed step stops the run, and the result still lists only what ran. `run` exits 0 on
`success` and 1 otherwise.

`merge --output DIR RUN...` combines run directories into `inputs.json` and `results.json` for
`coverage_campaign qualify`. Two runs that bind the same cell, or carry different candidates or
matrices, are an error (exit 2).

### Node — `tests/integration/test_host_install_live.py`

`test_installed_host_boots_pinned_kernel` is marked `live_stack`. It skips unless
`HOST_INSTALL_PHASE`, `HOST_INSTALL_OUTPUT`, `HOST_INSTALL_BUNDLE` and `HOST_INSTALL_CANDIDATE`
are set. It runs from `~/kdive`, with `examples/local-libvirt/env.sh` sourced, and writes
`<output>/phase.json`. It makes these observations:

- **Deployed revisions.** `server`, `reconciler` and `worker` come from `probe_stack_skew`. Each
  reported commit is resolved to a full SHA in the checkout. Every reporting worker must agree,
  and the checkout must be `HEAD == candidate` with no modified source.

  `authority` is taken from `/opt/kdive-live-worker-lifecycle/revision`. It counts only when the
  digest of the installed venv's `kdive` package `.py` files equals the digest of checkout
  `src/kdive`. The stamp alone can lie.
- **Installed prerequisites.** The lifecycle venv imports `guestfs`, `libvirt` and `kdive` under
  `-I`. `systemctl is-active kdive-live-worker-lifecycle.socket` reports active. The operator
  belongs to `kdive-live-control`. `KDIVE_LIBVIRT_URI` names the published session endpoint.
- **Boot.** The node runs `allocations.request` and `systems.provision` with the local rootfs and
  `boot_method: direct-kernel`, and waits for `ready`. It then runs `investigations.open` and
  `runs.create`, uploads `kernel` and `effective_config`, and runs `runs.complete_build`,
  `runs.install` and `runs.boot`. The boot passes only if the run's console artifact contains
  `Linux version <manifest release>`.
- **Confinement.** While the guest runs, the node reads the security label of the qemu process
  whose command line names the system's domain (`ps -eo label,args`). The label must contain
  `svirt_t` under SELinux, or name an AppArmor profile other than `unconfined`. The host mode is
  re-read as well.
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

`phase.json` records `passed`, `phase`, `deployed`, `context`, and per-check observations and
failures. The node asserts `passed`. A missing `phase.json` counts as a failed phase.

### Wiring

- `obligations.toml` gets one additive line in `[implementations]`, mapping `"host-install"` to
  the node.
- The docs gain the runner usage and output contract (coverage qualification), the unattended
  become behaviour (install), and the proven family status (local-libvirt).
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
  a role mismatch or a context mismatch. They yield `blocked` for a non-clean host. They emit
  only quoted values in step scripts.
- `coverage-check` passes with the node bound.
- Each of the three families has a run directory whose cell verdict under `qualify` is
  `success`, or `failure`/`blocked` with a linked issue. The proof record holds the per-cell
  outcomes, step durations and the sanitized identities.

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
