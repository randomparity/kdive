# Proof record — clean and repeated host installation (#2807)

- **Issue:** #2807 · **Decision:** [ADR-0716](../adr/0716-host-install-evidence-producer.md)
  · **Design:** [spec](../workflow/specs/2026-10-01-host-install-proof-design.md)
- **Candidate:** `f119602a818e4106d4067fd7a542cc64c2a58d66`. Server, reconciler and worker
  reported this revision on every host that booted. Commits after it on the branch change only
  documentation.
- **Matrix:** `58995223c12ae6a536364c29ccd2be39edb7d86b6278682c9da3ba3f46b88cb5`
- **Date:** 2026-10-02 (UTC).
- **Hosts:** exclusive, snapshot-capable disposable lab test hosts with nested KVM, one per
  family. In this record they are `lab-ubuntu` (Ubuntu 26.04, AppArmor enabled), `lab-fedora`
  (Fedora 44, btrfs root, SELinux enforcing) and `lab-rocky` (Rocky Linux 10.2, SELinux
  enforcing). Each had 8 vCPUs, 32 GiB RAM and a 256 GiB disk. The hosts ran one at a time.
  Each was restored to its verified `clean` snapshot through the lab's restore tooling
  immediately before its run and again afterwards; its higher snapshots stayed intact. Nothing
  was changed by hand before a run.
- **Kernel bundle:** the pinned `longterm` fixture: `v6.18.54`, commit
  `1b357ecb321392158d507b04672ffee57bfa071d`, release `6.18.54-g1b357ecb3213`, ELF build ID
  `6eda386b6100539e05faf0928f99b2a127ad6bf2`, fixture ID
  `a55c1f964b781dc6648dcf3e9d1ca41ee3ea68d9bbc40d799369d3f3da73131d`. The bundle was cut with
  `scripts/host_install_proof.py bundle`, and its `boot/vmlinuz` SHA-256 is
  `e8b71f57b03b7eafd91bb37670cf1a912ad427a63c7e2838c8b14627892c615d`. The build host also had
  `dpkg-query` installed, so the fixture was built with that tool hidden; see #3063.
- **Guest image:** `fedora-kdive-ready-44` on `lab-ubuntu` and `lab-fedora`, and
  `rocky-kdive-ready-10` on `lab-rocky`. Each host built its own image with `build-image.sh`.

## Per-cell outcome

`coverage_campaign qualify` over the merged runs at the candidate:

| Required cell | Outcome | Qualified | Reasons | Linked cause |
|---|---|---|---|---|
| `host-install/local-libvirt/x86_64/fedora` | success | yes | none | — |
| `host-install/local-libvirt/x86_64/debian` | failure | no | reported-failure (`confinement`) | #3067 |
| `host-install/local-libvirt/x86_64/enterprise` | failure | no | deployed-role-missing, required-input-missing (stopped at the operator prerequisites) | lab baseline; then #3068 |

The three ppc64le cells share the bound node and stay `not-run` / `missing-result` until #2818
runs them on native POWER.

The host-install cells require the `server`, `worker` and `reconciler` revisions (#3066). The
node reads all deployed roles through the shared ADR-0715 reader. These hosts run no provider
authority, so `authority` is absent; a reported one would have to equal the candidate. The
installed lifecycle witness is checked separately, as a prerequisite inside `confinement`. Its
revision stamp and its venv's code digest both matched the candidate on both hosts that booted.

## What each run proved

| Assertion | lab-fedora | lab-ubuntu | lab-rocky |
|---|---|---|---|
| `clean-install` | holds | holds | fails: operator prerequisites (Docker would not start) |
| `first-boot` | holds | holds | not reached |
| `repeat-setup` | holds | holds | not reached |
| `second-boot` | holds | holds | not reached |
| `confinement` | holds: `svirt_t` with per-boot MCS pairs (`s0:c262,c492`, `s0:c488,c746`) | fails: guest qemu `unconfined` in both boots | not reached |
| `cleanup` | holds: System `torn_down`, domain absent, both boots | holds | not reached |
| Deployed `server`/`worker`/`reconciler` | candidate, both phases | candidate, both phases | not reached |

- **lab-ubuntu** booted the bundle kernel in both boots (the console showed
  `Linux version 6.18.54-g1b357ecb3213`), and its worker prerequisites held. Guest confinement
  failed: the session libvirt daemon reports security model `none`, so qemu runs `unconfined`
  while host AppArmor stays enabled (#3067).
- **lab-rocky** stopped in its operator prerequisites. Its `clean` snapshot runs kernel
  `6.12.0-211.16.1`, the repositories carry `kernel-modules-extra` only for newer kernels, and
  Docker CE could not start. An earlier run on the same guest recorded the cause in the Docker
  journal: `iptables ... -m addrtype ...: Extension addrtype revision 0 not supported, missing
  kernel module?`. This is the lab baseline's state, not a KDIVE defect.
  A lab run on the same guest, with packages upgraded and the guest rebooted first, got past
  this point. It then stopped in `just prepare-local-libvirt-host`, where the EL10 guestfs
  binding build enabled Docker's `docker-ce-stable-source` repository (#3068).

## Step durations (seconds)

| Step | lab-fedora | lab-ubuntu | lab-rocky |
|---|---|---|---|
| operator prerequisites | 12 | 8 | 74 (failed) |
| bootstrap + just | 5 | 1 | — |
| kernel source | 56 | 56 | — |
| `just setup` | 99 | 89 | — |
| `prepare-local-libvirt-host` | 105 | 100 | — |
| `demo-up.sh` | 205 | 185 | — |
| `build-image.sh` | 74 | 74 | — |
| first boot (node) | 90 | 119 | — |
| repeat setup + prepare + stack | 93 | 90 | — |
| second boot (node) | 104 | 132 | — |
| **whole run** | **846** | **856** | **74** |

## Earlier runs

Earlier full runs at intermediate branch revisions reached the same outcome for each family.
They ran on these lab hosts and, before the lab hosts were reachable, on disposable VMs built
from the catalog-pinned cloud images. They also fixed three runner defects:

- A bundle clone has no `origin/main`, and the setup hooks read it.
- A clean host has no kernel tree, and host preparation grants traversal only to a tree that
  exists.
- An AppArmor label was cut at its mode suffix.

## Operator prerequisite files

These are the only commands run outside KDIVE's documented entry points. The runner records
each file's SHA-256 in the `clean-install` artifact. Every line cites the operator duty that the
installation docs assign.

`lab-fedora`, SHA-256 `2cbcf729ce398e8c7075a1e260cc2b99448afd834923d6fdac402f06ec12ec1f`:

```sh
set -euo pipefail
# docs/operating/install.md "From source": Docker and Compose are required for developer setup;
# setup preserves an existing Docker installation and leaves daemon start and group access to the
# operator ("follow the reported access/startup remedy"). moby-engine is the package setup names.
sudo -n dnf install -y moby-engine
sudo -n systemctl enable --now docker
sudo -n usermod -aG docker "$USER"
```

`lab-ubuntu`, SHA-256 `7608bf04320884b16cac0a5448a6e135578ac188dcf023543599d2670a8084b1`:

```sh
set -euo pipefail
# docs/operating/install.md "From source": Docker and Compose are required for developer setup;
# setup preserves an existing Docker installation and leaves daemon start and group access to the
# operator ("follow the reported access/startup remedy"). docker.io is the package setup names.
sudo -n apt-get update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
sudo -n systemctl enable --now docker
sudo -n usermod -aG docker "$USER"
```

`lab-rocky`, SHA-256 `f667527aafb001d18e6a899e1fcf59640a2cb00dd69c10a37c6424825bbf0ad7`:

```sh
set -euo pipefail
# docs/operating/install.md "Local-libvirt host preparation", RHEL/Rocky row: enable CRB and a
# distribution source repository before host preparation.
sudo -n dnf install -y dnf-plugins-core
sudo -n dnf config-manager --set-enabled crb baseos-source appstream-source
# docs/operating/install.md "From source": distributions without a known Docker engine package
# require an operator-configured engine; setup preserves it. Docker's upstream EL repository.
sudo -n dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo -n dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo -n systemctl enable --now docker
sudo -n usermod -aG docker "$USER"
```

The runner also fetched the kernel tree that `examples/local-libvirt/README.md` lists as a
prerequisite, using the repository's own `scripts/fetch-kernel-tree.sh` at the bundle's source
commit. No package was installed by hand.
## Limits

- The clean baseline is the lab's verified `clean` snapshot, used unchanged.
- Evidence files, transcripts and phase records stay private. This record publishes only
  outcomes, durations and content identities.
- The qualifier checks accounting and identity, not whether a producer fabricated a digest
  (ADR-0686). The node and runner are the reviewed producers.
