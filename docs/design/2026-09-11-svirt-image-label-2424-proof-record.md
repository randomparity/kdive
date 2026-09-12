# Proof record — static `svirt_image_t` label for kdive images (#2424)

- **Issue:** #2424 · **Decision:** [ADR-0639](../adr/0639-static-svirt-image-label-for-session-mode-domains.md)
- **Branch build under test:** `5af70d10e`, confirmed on both targets before any measurement
  (`scripts/live-stack/status.sh` build stamps read `g5af70d10e` for server and reconciler after
  `up.sh` restarted them; they had been running the pre-branch build).
- **Date:** 2026-09-11 / 2026-09-12 UTC
- **Targets:** a Fedora 44 host and a Rocky 10.2 host, both `getenforce` = `Enforcing` throughout.
  No `setenforce 0`, and no `security_driver` override exists on either (`selinux` remains the
  libvirt default).

Completion criterion 6 of the frozen charter. Criteria 1, 2, 3 and 5 are discharged here;
criterion 4 was discharged by a design-time privileged-daemon probe recorded in ADR-0639;
criterion 7 is `just ci`.

## What each target proved, and what it could not

The two targets are not interchangeable, and neither alone discharges criterion 6.

| Arm | Rocky 10.2 | Fedora 44 |
|---|---|---|
| Rewrite a stale `virt_image_t` rule | **yes** — the only host that still had one | no — pre-labeled by design probes |
| Add a missing rule | yes (`install`) | yes (`install`) |
| Re-run against an already-correct rule | — | **yes** (`rootfs`) |
| Provision a System to `ready` | **no** — blocked, see below | **yes** |
| Confined domain + zero denials | — | **yes** |
| Install-staging map | — | **yes** (constructed probe) |
| `rootfs/local` read-only backing | — | **yes** (constructed probe) |

## 1. Host preparation — `install-host.sh`, run from the branch checkout

### Rocky 10.2 — the migration path

Pre-state: exactly one local rule, `/var/lib/kdive/rootfs(/.*)?` → `virt_image_t`;
`/var/lib/kdive/install` unlabeled (`var_lib_t`). This is an upgraded host as #2424 describes.

`install-host.sh` exited 0 and printed, under its SELinux step:

```
=== SELinux svirt_image_t on the kdive image directories ===
File context for /var/lib/kdive/rootfs(/.*)? already defined, modifying instead
```

That line is the measurement the single-`semanage fcontext -a` design rests on. Until this run it
was a source read of `seobject.fcontextRecords.add()`; here the real tool took the rewrite path on
policycoreutils-python-utils 3.10 (el10). `/var/lib/kdive/install(/.*)?` printed nothing — the add
path, no prior rule.

Post-state: exactly **two** rules, one per owned pattern, both `svirt_image_t`, no duplicates —
**Success criterion 3**. All of `/var/lib/kdive/rootfs`, `/var/lib/kdive/install` and
`/var/lib/kdive/rootfs/local` came out `svirt_image_t`; `local/` inherited the parent rule through
`restorecon -R`, because this host has no nested rule of its own.

### Fedora 44 — the add path and re-run safety

Pre-state: `rootfs` and `rootfs/local` rules already `svirt_image_t` (left by design-phase probes);
`/var/lib/kdive/install` unlabeled. `install-host.sh` exited 0; `rootfs(/.*)?` printed
`already defined, modifying instead` again — here demonstrating **idempotence**, not migration —
and `install(/.*)?` was silent. `/var/lib/kdive/install` moved `var_lib_t` → `svirt_image_t`.
Post-state: **three** rules, one per pattern, no duplicates.

## 2. Provisioning under enforcing — Fedora 44

A System was provisioned through the ordinary worker path (`scripts/live-vm/mint-system.sh`:
onboard → allocate → provision → poll ready) against a **staged prebuilt image**.
`build-image.sh` was **not** run — see §5.

- System `1a709aa0-3e21-4244-bd89-19d12fdf1a02` reached **`ready`**.
- QEMU process context: `unconfined_u:unconfined_r:svirt_t:s0:c1000,c1021` — confined, with
  per-domain MCS categories. **Criterion 3.**
- `…-overlay.qcow2` → `system_u:object_r:svirt_image_t:s0` — **criterion 1**.
- `…-baseline/kernel` and `…-baseline/initrd` → `system_u:object_r:svirt_image_t:s0` —
  **criterion 2, provisioning half**.
- **Zero `avc: denied` records of any kind** in the kernel journal across the provisioning window
  (read from the journal, not `ausearch`, which does not surface these on either target).

The live XML is the direct confirmation of ADR-0639's model:

```xml
<seclabel type='dynamic' model='selinux' relabel='yes'>
  <label>unconfined_u:unconfined_r:svirt_t:s0:c1000,c1021</label>
  <imagelabel>unconfined_u:object_r:svirt_image_t:s0:c1000,c1021</imagelabel>
</seclabel>
```

libvirt *generated* a per-domain `imagelabel`, yet the files on disk stayed at plain
`svirt_image_t:s0`. The relabel did not happen, and the domain works by MCS dominance — which is
the whole reason the static label has to be one `svirt_t` can use.

### Why the relabel does not happen — measured, and narrower than "a session daemon never relabels"

Two files in the same run, one relabeled and one not, differing only in ownership:

| File | Owner | Label while the domain ran |
|---|---|---|
| `…-baseline/kernel` (worker-staged) | `kdive-worker-1` | `svirt_image_t:s0` — **unchanged** |
| a kernel staged by the daemon's own user | the operator | relabeled to `virt_content_t:s0` |

The unprivileged daemon relabels what it has permission to relabel and nothing else. In this
deployment the images are created by the fixed `kdive-worker-N` accounts while the daemon runs as
the operator, so it cannot relabel them — which is precisely why the **static** label is
load-bearing. ADR-0639 records this refinement.

## 3. Install staging — criterion 2, second half

Provisioning never reaches the install plane, so this arm was measured directly rather than left
to an assertion. `kernel` and `initrd` were staged under
`/var/lib/kdive/install/<system-id>/<run-id>/` exactly as `lifecycle/install.py` stages them, and a
domain was started with its `<os>` pointing at them.

- Staged files inherited `svirt_image_t`; `matchpathcon` agreed.
- The domain **started** and mapped them. Running concurrently with the provisioned System, the two
  confined domains held **distinct** MCS pairs (`s0:c1000,c1021` and `s0:c12,c1020`).
- **Zero denials.**

**Method caveat, stated because it bounds the claim:** this was a constructed probe driven by
`virsh`, not an install driven through the worker. It measures the policy question the charter
criterion is about — can a confined session-mode domain map kernel/initrd out of
`/var/lib/kdive/install` — and does not exercise `install.py`'s own staging code. A full install
needs a kernel build, whose failure modes are unrelated to labeling. The probe's files, being
operator-owned, were relabeled to `virt_content_t` before the map, per §2.

## 4. `rootfs/local` left at `virt_image_t` — the claim that was an inference

ADR-0639 leaves the nested `rootfs/local` rule to `build-image.sh`, on the grounds that base images
there are read-only backing files a `svirt_t` domain may read under either label. Every other
load-bearing claim in that record carried a measurement; this one carried an inference, and the
operator's decision on 2026-09-11 was to keep the cut and measure it here.

A first attempt was inconclusive and is recorded as such: with an operator-owned base at
`virt_image_t`, libvirt relabeled it to `virt_content_t` before the domain read it, so the domain
starting proved only that libvirt copes.

The clean measurement used a base the daemon **cannot** relabel — root-owned, mode `0644`, set to
`virt_image_t` — with an overlay backing onto it:

| File | Label before | Label while the domain ran |
|---|---|---|
| base (root-owned) | `virt_image_t:s0` | `virt_image_t:s0` — **unchanged** |
| overlay | `svirt_image_t:s0` | `svirt_image_t:s0:c16,c599` |

The domain **started and ran, with zero denials**. A base image left at `virt_image_t` does serve
as a read-only backing file for a confined domain. The claim is now measured, and
`install-host.sh` does not need to own the nested rule.

## 5. `build-image.sh` was not exercised — and why

Stated plainly because "it succeeded" and "it was not run" are different evidence.

The build-time customization boot opens the session daemon against a workspace defaulting under
`$HOME`, where `svirt_t` has neither write nor map on `data_home_t` and write-but-not-map on
`svirt_home_t`. That is a second instance of the #2424 denial class on the build path, out of scope
for this change and tracked as [#2428](https://github.com/randomparity/kdive/issues/2428). Running
it here could have failed for that reason and been misread as a failure of this change, so the
proof staged a prebuilt image instead.

The plan's non-gating diagnostic — run `build-image.sh` once on the Fedora target *after* the
gating arms, to settle whether the provider page's "builds end to end" row or the measured policy
is stale — **was not run**. It is not required by any completion criterion, and the question it
settles belongs to #2428.

## 6. Rocky 10.2 could not provision — blocker named, not papered over

```
worker venv cannot 'import guestfs, drgn'                        (install-host.sh preflight FAIL)
systems.get error: missing_dependency — libguestfs (the guestfs Python binding) is
                   required to extract the baseline kernel
```

Measured cause: system Python is **3.12.14**, the venv is **3.14.7**, and
`python3-libguestfs-1.58.1-9.el10_2` is built for 3.12. `install-host.sh` skips the binding symlink
on a minor mismatch by design, and said so during this very run. No overlay is created, so criteria
1 and 2 cannot be observed on this host.

**This is not caused by this change**, and the negative evidence supports that: no `avc: denied`
naming any kdive path appeared at any point on this target. The labeling arms all passed here; it
is the provisioning arm that is unavailable, and Fedora 44 carries it.

This exposed a documentation defect, fixed in the same change: the example README said the Python
minor mismatch "stays a `WARN` (kdump only) and everything else works". It is not kdump-only — it
blocks baseline kernel extraction during provisioning, so on EL10 provisioning cannot complete at
all.

## 7. Host state left behind

- Both targets: `setools-console` installed during design (read-only analysis tool), left in place.
- Both targets: checkouts switched to `fix/svirt-image-label-2424`; stacks restarted on that build.
- Fedora 44: the leftover permissive-mode domain and its orphaned System row were destroyed,
  undefined and removed — it started under permissive during design and is not a counterexample.
  Its stale `active` allocation was moved to `released` so the host cap freed.
- Fedora 44: System `1a709aa0-…` is **left running and `ready`**. Probe domains `probe2424`,
  `probe7` and `probe8` and all their scratch files were destroyed, undefined and deleted;
  `virsh list --all` shows only the provisioned System.
- Fedora 44: restoring the base image's label needed `restorecon -F` — a plain `restorecon`
  refused with *"not reset as customized by admin"*, which is live confirmation of the
  customizable-type caveat ADR-0639 records as the rollback lever.
