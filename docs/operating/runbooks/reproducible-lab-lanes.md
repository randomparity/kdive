# Reproducible lab lanes for issue #2805

This preparation supplies an x86 fixture and separate readiness evidence for native x86,
foreign-architecture TCG, and native POWER. It does not qualify a live scenario or deploy the
server, worker, or reconciler. Use [live testing](live-testing.md) for those later tiers. Target
names below are aliases; the operator keeps connection details and raw logs in a private inventory.

## Capacity and assignments

| Lane | Observed host | Preparation at candidate `0876df4117` |
| --- | --- | --- |
| `lab-x1` | Fedora 44, x86_64, 32 CPUs, about 126 GiB RAM | Native x86 and ppc64le TCG prerequisite checks passed; no fixture build |
| `lab-x2` | Ubuntu 26.04, x86_64, 32 CPUs, about 121 GiB RAM | Native x86 and ppc64le TCG prerequisite checks passed; cold/warm x86 fixture built |
| `lab-x3` | Rocky 10.2, x86_64, 32 CPUs, about 125 GiB RAM | Inventoried only; virtualization stack and preparation remain unverified |
| `lab-x4` | openSUSE Tumbleweed, x86_64, 32 CPUs, about 63 GiB RAM | Inventoried only; virtualization stack and preparation remain unverified |

The operator approved task-owned reservations on these x86 targets, with at most one fixture build
per host and no host OS reprovisioning. Only `lab-x1` and `lab-x2` were prepared. Each has an
operator-owned, physically preallocated 64 GiB ext4 loop filesystem with `nodev,nosuid`; the
scratch root is mode `0700`. The lane capacity check asks for **8 available CPUs, 16 GiB
available RAM, and 48 GiB currently free** on that mounted workspace. It is an admission floor,
not a reservation: check immediately before each build and stop when retained data leaves less
than 48 GiB. The 64 GiB filesystem bounds task scratch; do not expand it as a way around a failed
check. Put `TMPDIR`, `LIBGUESTFS_TMPDIR`, `LIBGUESTFS_CACHEDIR`, the rootfs/console roots, and the
store on the same mount. The observed customization guest used 2 vCPU and 2048 MiB; libguestfs
was set to 2048 MiB. The 8 CPU/16 GiB admission floor leaves headroom for host tools and several
copies of a 6 GiB guest disk. Build one fixture at a time.

## Preparing and checking a lane

Use a fresh external worktree at the candidate commit with a locked Python 3.14 environment. The
interpreter must import `kdive` from that same clean checkout. The existing `libvirt_stack` role
supplies virtualization; `local_worker_host` supplies its family package tasks, `uv.yml`, and
`boot_kernels.yml`. On Fedora, the RedHat package list includes `python3-devel` for the system
Python headers used when `uv sync --locked` builds `libvirt-python`. The current Fedora package
repository supplied `python3-devel-3.14.7-1.fc44.x86_64`; after the role installed it, the
locked sync built `libvirt-python==12.5.0`. Keep SELinux/AppArmor enabled under the existing roles.
Do not apply the worker lifecycle roles just to build a fixture.

Run each prerequisite arm separately so one result does not stand in for another:

```sh
export KDIVE_PYTHON="$PWD/.venv/bin/python"
export KDIVE_LIBVIRT_URI=qemu:///session
export XDG_CONFIG_HOME=<task-mount>/xdg
export KDIVE_LANE_WORKSPACE=<task-mount>
export KDIVE_LANE_DISK_BYTES=51539607552
bash scripts/live-vm/preflight-env.sh native-x86
bash scripts/live-vm/preflight-env.sh tcg-host
bash scripts/live-vm/preflight-env.sh capacity
bash scripts/live-vm/preflight-env.sh native-power
```

The last command is expected to fail on these x86 hosts because native POWER requires a ppc64le
host with KVM-HV. `tcg-host` confirms that local libvirt reports an executable ppc64le TCG
emulator; it does not boot a POWER guest. Native POWER execution and hardware allocation remain
with #2818. A checksum-pinned POWER cloud image is available for that owner, but no POWER fixture
was customized or qualified here.

## Selected inputs and fixture evidence

| Catalog row | Architecture | Pinned source SHA-256 | Outcome |
| --- | --- | --- | --- |
| `fedora-kdive-ready-44` | x86_64 | `28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f` | Cold build and warm reuse passed |
| `fedora-kdive-ready-44-ppc64le` | ppc64le | `3bea270eba46cdedf3c6c71b20c6ffb03b54131631497a27ff826a497c1dfac6` | Source verified; staging pending |

Build the x86 row with `KDIVE_WARM_STORE_IMAGE=fedora-kdive-ready-44`,
`KDIVE_WARM_STORE_TARGET_NVR=6.19.10-300.fc44.x86_64`, the checkout's
`KDIVE_PYTHON`, and Fedora's `DEBUGINFOD_URLS`; run `scripts/live-vm/warm-store.sh` first with
an empty task store and then again unchanged. The builder reacquires the pinned source inside its
fresh build workspace; a prior downloaded input is not the cold proof. A libguestfs appliance
cache may already exist and is independent of the fixture store. A warm hit validates the whole
manifest and artifact bytes, including the selected catalog row and clean builder commit. Legacy
or stale manifests rebuild; a changed catalog input or builder also invalidates reuse. Mutable
guest package repositories can yield a new fixture ID on rebuild and require requalification.
Source-built kernel baselines belong to #2806.

On `lab-x2`, a clean detached checkout at commit `0876df41170b0cf6a87e3ef63b5bdbce853a7366`
produced fixture ID `8b79d245dd76669fb75fd7cfe6ccfa1dbe011884faa80cee3a66530fc7704e27`.
The cold run completed in 176.3 seconds. Its peak added scratch use was 2,932,412,416 bytes;
the final staged set used 1,941,205,052 bytes. Free space started at 64,096,755,712 bytes,
reached a measured minimum of 61,164,343,296 bytes, and ended with 62,155,431,936 bytes free. The transient
customization domain's XML reported 2 vCPU and 2 GiB. No domain existed before or after the
build. Warm reuse took 5.9 seconds, added no measured scratch bytes, started no domain, and
kept the same fixture ID and artifact digests.
This retained fixture belongs to builder commit `0876df4117`. The subsequent documentation
commit changes the builder revision; using that later checkout will rebuild and assign a new
fixture identity before downstream qualification.

The retained kernel and debuginfo both have GNU build ID
`ac46f5009041c93426043e26daffa422db708cfc`. The manifest records Fedora 44 kernel
`6.19.10-300.fc44.x86_64`, the selected source checksum, all five artifact SHA-256 digests,
the package version map, kernel config, and a source provenance sidecar. Its retained artifact
digests are:

| Artifact | SHA-256 |
| --- | --- |
| `rootfs.qcow2` | `93e1f99e7611057b1569add524d99b407cdd47ff238bf6a2ea612f0f02191208` |
| `rootfs.qcow2.config` | `3ef1ef52ff1af63652d5950522cbd908c85865d80c351f0329685440e2e01e7f` |
| `rootfs.qcow2.provenance.json` | `7b3b92bf0676d7bfaa9da5512b00ca575b05d69eb724fcbd3083cdf42e4ee876` |
| `vmlinux` | `4b37e4e542a62c580c751787848be6c99e6f908f6712c8c6da85516b8d541de2` |
| `vmlinux.debug` | `edb259b36c047d0bbe0b8ad09daa7e0589f833df89487635b6a037cd42208dff` |

The sidecar retains package versions for `drgn`, `kdump-utils`, `kexec-tools`, `keyutils`,
`makedumpfile`, and `openssh-server`, plus Fedora release, root specification, and source digest.
The config sibling is present and hashed. The fixture and both 64 GiB reservations remain under
operator ownership for downstream qualification. Only task-created transient build inputs were
removed by the builder. No shared domain or storage was removed. Recheck the capacity floor and
fixture identity before a downstream live run; this preparation alone is not live qualification.
