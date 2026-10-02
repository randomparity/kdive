# Proof record — clean and repeated host installation (#2807)

- **Issue:** #2807 · **Decision:** [ADR-0716](../adr/0716-host-install-evidence-producer.md)
  · **Design:** [spec](../workflow/specs/2026-10-01-host-install-proof-design.md)
- **Candidate:** `0ac30cfe74f461930bbc15a15b5418ddb0c3c6a7`. Every role on every host reported
  this revision. Commits after it on the branch are documentation plus a merge of `main` that
  brought in the image-smoke binding (#2808). That merge changes the matrix identity, so this
  evidence qualifies the candidate and matrix named here, not a later head.
- **Matrix:** `fc53e68775e6c519ac5f72e3b4e89f1bc8fd79836f4d654cb4ca5513e5729740`
- **Date:** 2026-10-01 to 2026-10-02 (UTC).
- **Hosts:** exclusive, snapshot-capable disposable lab test hosts with nested KVM, one per
  family. In this record they are `lab-ubuntu` (Ubuntu 26.04, AppArmor enabled), `lab-fedora`
  (Fedora 44, btrfs root, SELinux enforcing) and `lab-rocky` (Rocky Linux 10.2, SELinux
  enforcing). Each had 8 vCPUs, 32 GiB RAM and a 256 GiB disk.

  Each host was restored to its verified `clean` snapshot through the lab's restore tooling
  before its run, and again afterwards. Before its run, `lab-rocky` also had `dnf upgrade`
  applied and was rebooted. Its clean image runs a kernel whose matching
  `kernel-modules-extra` the repositories no longer carry, so Docker cannot start on it
  unchanged.
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

| Required cell | Outcome | Qualified | Reasons | Linked defect |
|---|---|---|---|---|
| `host-install/local-libvirt/x86_64/fedora` | success | yes | none | — |
| `host-install/local-libvirt/x86_64/debian` | failure | no | reported-failure (confinement) | #3067 |
| `host-install/local-libvirt/x86_64/enterprise` | failure | no | deployed-role-missing, required-input-missing (stopped at host preparation) | #3068 |

The three ppc64le cells share the bound node and stay `not-run` / `missing-result` until #2818
runs them on native POWER.

## What each run proved

| Assertion | lab-fedora | lab-ubuntu | lab-rocky |
|---|---|---|---|
| `clean-install` | holds | holds | fails: `just prepare-local-libvirt-host` exit 2 |
| `first-boot` | holds | holds | not reached |
| `repeat-setup` | holds | holds | not reached |
| `second-boot` | holds | holds | not reached |
| `confinement` | holds: `svirt_t` with per-boot MCS pairs (`s0:c671,c900`, `s0:c155,c170`) | fails: guest qemu `unconfined` in both boots | not reached |
| `cleanup` | holds: System `torn_down`, domain absent, both boots | holds | not reached |
| Deployed `server`/`worker`/`reconciler`/`authority` | candidate, both phases | candidate, both phases | not reached |

The authority revision counts only when the installed lifecycle venv's `kdive` package source
digest equals the checkout's `src/kdive`. On `lab-ubuntu`, both boots booted the bundle kernel:
the console showed `Linux version 6.18.54-g1b357ecb3213`, and the worker prerequisites held.
The only failed check was guest confinement. The session libvirt daemon reports security model
`none`, so qemu runs `unconfined` while host AppArmor stays enabled (#3067). On `lab-rocky`, the
EL10 guestfs binding build enabled Docker's `docker-ce-stable-source` repository through
`--enablerepo='*source*'`, and its metadata download failed (#3068).

An earlier full run at candidate `a5a3818754d048cafddfda1cfdc2d2b095c4bc76` reached the same
three outcomes for the same reasons. It ran on disposable VMs created from the catalog-pinned
vendor cloud images while the lab hosts were unreachable.

## Step durations (seconds)

| Step | lab-fedora | lab-ubuntu | lab-rocky |
|---|---|---|---|
| operator prerequisites | 12 | 10 | 42 |
| bootstrap + just | 5 | 1 | 5 |
| kernel source | 57 | 57 | 55 |
| `just setup` | 118 | 111 | 101 |
| `prepare-local-libvirt-host` | 102 | 101 | 99 (failed) |
| `demo-up.sh` | 229 | 212 | — |
| `build-image.sh` | 80 | 82 | — |
| first boot (node) | 113 | 111 | — |
| repeat setup + prepare + stack | 96 | 102 | — |
| second boot (node) | 103 | 132 | — |
| **whole run** | **920** | **922** | **304** |

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

## Rehearsals that changed the runner

Three earlier runs at intermediate branch revisions failed for reasons in the runner itself, and
were fixed before the final runs:

- A bundle clone has no `origin/main`, and the setup hooks read it. The clone now fetches it.
- A clean host has no kernel tree, and host preparation grants traversal only to a tree that
  exists. The runner now fetches one before setup.
- An AppArmor label was cut at its mode suffix. The node now reads the whole label from `/proc`.

Those rehearsals produced the same per-family conclusions as the final runs.

## Limits

- The clean baseline is the lab's verified `clean` snapshot. `lab-rocky` additionally received
  a package upgrade and reboot, as described above.
- Evidence files, transcripts and phase records stay private. This record publishes only
  outcomes, durations and content identities.
- The qualifier checks accounting and identity, not whether a producer fabricated a digest
  (ADR-0686). The node and runner are the reviewed producers.
