# ppc64le catalog parity (#1152) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the local-libvirt rootfs catalog to ppc64le parity by adding sha256-pinned
ppc64le siblings for the rhel-family rows whose distro publishes a ppc64le GenericCloud qcow2,
documenting and testing the N/A gaps, and proving one non-Fedora row end-to-end under TCG.

**Architecture:** Data + tests only in the required path — the catalog is file-authoritative TOML
(`fixtures/local-libvirt/rootfs_catalog.toml`, ADR-0251) and the loader (`catalog.py`) already
carries `arch`/`source`/version fields, so no schema change. Tests are added first (TDD red) then
the rows (green). One live TCG customize-boot proof (CentOS Stream 9) closes the acceptance
criterion; a conditional EL9 EPEL customizer fix is applied only if the proof forces it.

**Tech Stack:** Python 3.14, `uv`, `pytest`, `ruff`, `ty`; TOML catalog; libguestfs/QEMU
(`qemu-system-ppc64` TCG) for the live proof.

## Global Constraints

- Spec: `docs/design/2026-07-14-ppc64le-catalog-parity-1152.md`; ADR:
  `docs/adr/0350-ppc64le-catalog-parity.md`. Every design decision lives there.
- `BASE_BRANCH = main`; branch `feat/ppc64le-catalog-parity-1152`.
- Guardrails: `just lint` (`ruff check` + `ruff format --check`), `just type` (`ty`, whole tree),
  `just test` (excludes `live_vm`), and the full `just ci` before push. Run a single test with
  `uv run python -m pytest tests/images/test_rootfs_catalog.py::<name> -q`.
- Naming rule: each new row is `<x86_64-row-name>-ppc64le`, `family = "rhel"`,
  `arch = "ppc64le"`, `source.kind = "cloud-image"`.
- `makedumpfile_version` / `drgn_version` **mirror the x86_64 sibling** (arch-invariant within a
  release); the EL9 `drgn_version` is confirmed against the actually-installed version recorded by
  the live proof if the EPEL index is not enumerable.
- Every `cloud-image` `url` must carry a `  # pragma: allowlist secret` trailing comment on the
  `source = { … }` line (the detect-secrets hook flags the 64-hex sha256 otherwise — see the
  existing rows).
- Doc style: no "critical/crucial/essential/significant/comprehensive/robust/elegant/sprint";
  use "Milestone" not "Sprint".
- Exact pinned images and sha256 (probed 2026-07-14, checksums fetched from the distro CHECKSUM /
  `.SHA256SUM` files):

  | row | url | sha256 |
  |---|---|---|
  | `fedora-kdive-ready-43-cloud-ppc64le` | `https://dl.fedoraproject.org/pub/fedora-secondary/releases/43/Cloud/ppc64le/images/Fedora-Cloud-Base-Generic-43-1.6.ppc64le.qcow2` | `50c51fb73f01722b737031c333e92c0fe5bd0c3760421fe104bd888941cd41bb` |
  | `rocky-kdive-ready-9-ppc64le` | `https://dl.rockylinux.org/pub/rocky/9/images/ppc64le/Rocky-9-GenericCloud-Base-9.8-20260525.0.ppc64le.qcow2` | `811376d2a8126b0ebbf649179da8e0075a619e6208d3e14932da7f9a782f83bc` |
  | `rocky-kdive-ready-10-ppc64le` | `https://dl.rockylinux.org/pub/rocky/10/images/ppc64le/Rocky-10-GenericCloud-Base-10.2-20260525.0.ppc64le.qcow2` | `06ab97a625392776028dde8ef5d663550ca6e6e162efc5993fd398f59477bd42` |
  | `centos-stream-kdive-ready-9-ppc64le` | `https://cloud.centos.org/centos/9-stream/ppc64le/images/CentOS-Stream-GenericCloud-9-20260622.0.ppc64le.qcow2` | `11f12407e0f19f067fd90692f66b5c0c9e75fce8503c3e1d5cfe20268b8e0ec8` |
  | `centos-stream-kdive-ready-10-ppc64le` | `https://cloud.centos.org/centos/10-stream/ppc64le/images/CentOS-Stream-GenericCloud-10-20260622.0.ppc64le.qcow2` | `6d1ac346df9931c54fc8e88f496427d1123f1f5018b3ce8cd5ff307b835d217b` |

  Version fields to mirror (from the x86_64 siblings in the current catalog):
  Fedora 43-cloud → makedumpfile `1.7.8`, drgn `0.2.0`; Rocky 9 → `1.7.6` / `0.0.33`;
  Rocky 10 → `1.7.8` / `0.0.33`; CentOS Stream 9 → `1.7.6` / `0.0.33`;
  CentOS Stream 10 → `1.7.8` / `0.0.33`.

---

## File Structure

- `fixtures/local-libvirt/rootfs_catalog.toml` — add 5 `[[image]]` rows; add N/A comments (Rocky 8,
  Debian) and the build-host scope note. The single file that defines catalog rows (grep confirmed
  no seed-data/fixture mirror references row names).
- `tests/images/test_rootfs_catalog.py` — extend `_EXPECTED_MAKEDUMPFILE` / `_EXPECTED_DRGN`, add a
  version-parity test, a per-row ppc64le assertion, and an N/A-guard test.
- `docs/design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md` — created in Task 5 (live
  proof outcome), matching the epic's proof-record convention.
- `src/kdive/images/families/rhel.py` — **only if Task 5 surfaces the EL9 EPEL gap** (Task 6).

---

## Task 1: URL-resolve check (fail fast on a pruned serial)

**Files:**
- No repo files changed — this is a documented preflight for the data rows, run once.

**Interfaces:**
- Consumes: the Global-Constraints URL/sha256 table.
- Produces: confirmation each pinned URL resolves (HTTP 200) before rows are committed.

- [ ] **Step 1: HEAD-check every pinned ppc64le URL**

Run:
```bash
for u in \
  "https://dl.fedoraproject.org/pub/fedora-secondary/releases/43/Cloud/ppc64le/images/Fedora-Cloud-Base-Generic-43-1.6.ppc64le.qcow2" \
  "https://dl.rockylinux.org/pub/rocky/9/images/ppc64le/Rocky-9-GenericCloud-Base-9.8-20260525.0.ppc64le.qcow2" \
  "https://dl.rockylinux.org/pub/rocky/10/images/ppc64le/Rocky-10-GenericCloud-Base-10.2-20260525.0.ppc64le.qcow2" \
  "https://cloud.centos.org/centos/9-stream/ppc64le/images/CentOS-Stream-GenericCloud-9-20260622.0.ppc64le.qcow2" \
  "https://cloud.centos.org/centos/10-stream/ppc64le/images/CentOS-Stream-GenericCloud-10-20260622.0.ppc64le.qcow2"; do
  printf '%s -> ' "$u"; curl -sIL --max-time 30 "$u" | grep -m1 -oE 'HTTP/[0-9.]+ [0-9]+'; done
```
Expected: each line ends in `200`.

- [ ] **Step 2: If any is not 200** — re-probe the distro's ppc64le image directory for the current
  serial, update the Global-Constraints table URL + refetch its sha256 from the sibling CHECKSUM /
  `.SHA256SUM` file, and record the substitution in the spec's availability table. Do **not**
  proceed with a stale URL.

No commit (verification-only task).

---

## Task 2: Failing catalog tests for the ppc64le siblings (TDD red)

**Files:**
- Modify: `tests/images/test_rootfs_catalog.py`

**Interfaces:**
- Consumes: `load_rootfs_catalog()`, `CloudImageSource` (already imported in the test module);
  the Global-Constraints version table.
- Produces: the failing assertions Task 3 turns green (the five new names in
  `_EXPECTED_MAKEDUMPFILE` / `_EXPECTED_DRGN`, the parity test, the per-row test, the N/A guard).

- [ ] **Step 1: Add the five new rows to the two expectation dicts**

In `tests/images/test_rootfs_catalog.py`, extend `_EXPECTED_MAKEDUMPFILE` with:
```python
    "fedora-kdive-ready-43-cloud-ppc64le": "1.7.8",
    "rocky-kdive-ready-9-ppc64le": "1.7.6",
    "rocky-kdive-ready-10-ppc64le": "1.7.8",
    "centos-stream-kdive-ready-9-ppc64le": "1.7.6",
    "centos-stream-kdive-ready-10-ppc64le": "1.7.8",
```
and `_EXPECTED_DRGN` with:
```python
    "fedora-kdive-ready-43-cloud-ppc64le": "0.2.0",
    "rocky-kdive-ready-9-ppc64le": "0.0.33",
    "rocky-kdive-ready-10-ppc64le": "0.0.33",
    "centos-stream-kdive-ready-9-ppc64le": "0.0.33",
    "centos-stream-kdive-ready-10-ppc64le": "0.0.33",
```
Note: `test_only_below_threshold_rows_are_live_drgn_incapable` and
`test_only_fedora_44_is_capable_for_the_default_basis` iterate these dicts; the new EL9 rows ship
drgn ≥ 0.0.31 (live_drgn `capable`) and makedumpfile < 1.7.9 except none of the new rows are ≥
1.7.9, so all new rows are kdump `incapable` for the default basis — consistent with the existing
`_CAPABLE_ROWS = {"fedora-kdive-ready-44", "fedora-kdive-ready-44-ppc64le"}` guard, which stays
unchanged (do not add the new rows to `_CAPABLE_ROWS`).

- [ ] **Step 2: Add the version-parity + per-row + N/A-guard tests**

Append to `tests/images/test_rootfs_catalog.py`:
```python
_NEW_PPC64LE_ROWS = (
    "fedora-kdive-ready-43-cloud-ppc64le",
    "rocky-kdive-ready-9-ppc64le",
    "rocky-kdive-ready-10-ppc64le",
    "centos-stream-kdive-ready-9-ppc64le",
    "centos-stream-kdive-ready-10-ppc64le",
)


def test_new_ppc64le_rows_are_rhel_cloud_images() -> None:
    """Every new ppc64le sibling is an rhel-family sha256-pinned cloud image with the arch token."""
    cat = load_rootfs_catalog()
    for name in _NEW_PPC64LE_ROWS:
        entry = cat[name]
        assert entry.arch == "ppc64le", name
        assert entry.family == "rhel", name
        assert entry.kind == "debug", name
        src = entry.source
        assert isinstance(src, CloudImageSource), name
        assert src.url.endswith(".qcow2"), name
        assert "ppc64le" in src.url, name
        assert len(src.sha256) == 64, name


def test_ppc64le_rows_mirror_their_x86_64_sibling_versions() -> None:
    """A ppc64le row's makedumpfile/drgn versions equal its x86_64 sibling's (arch-invariant)."""
    cat = load_rootfs_catalog()
    for name in _NEW_PPC64LE_ROWS:
        sibling = name.removesuffix("-ppc64le")
        assert cat[name].makedumpfile_version == cat[sibling].makedumpfile_version, name
        assert cat[name].drgn_version == cat[sibling].drgn_version, name


def test_no_ppc64le_row_for_deferred_or_unported_distros() -> None:
    """N/A decision, executable: no ppc64le row for the debian family (deferred to #1167) or for
    Rocky 8 (no ppc64le port). A future addition of an un-buildable row fails here loudly.
    When #1167 adds a debian ppc64le row it must update this guard deliberately.
    """
    cat = load_rootfs_catalog()
    ppc = [e for e in cat.values() if e.arch == "ppc64le"]
    assert ppc, "expected ppc64le rows in the catalog"
    assert not [e for e in ppc if e.family == "debian"], "debian ppc64le is deferred to #1167"
    assert "rocky-kdive-ready-8-ppc64le" not in cat, "Rocky 8 has no ppc64le port"
```

- [ ] **Step 3: Run the new tests, verify they FAIL**

Run:
```bash
uv run python -m pytest tests/images/test_rootfs_catalog.py -q -k \
  "new_ppc64le_rows or mirror_their_x86_64 or no_ppc64le_row or makedumpfile_versions_match or drgn_versions_match"
```
Expected: FAIL — the five new names raise `KeyError` in `load_rootfs_catalog()`-backed lookups
(rows do not exist yet). The N/A-guard test PASSES already (no such rows yet); that is fine.

- [ ] **Step 4: Do NOT commit yet — leave the tests red in the working tree**

The `/work-issue` contract requires guardrails green **at every commit**. Committing the red tests
alone would leave `just test` red between this task and Task 3 (and break bisect). The local
red→green TDD cycle is preserved (tests written and observed failing here), but the *commit* happens
in Task 3 Step 5, which stages the tests **and** the rows together so the committed tree is green.
Keep the edited `tests/images/test_rootfs_catalog.py` in the working tree and proceed to Task 3.

---

## Task 3: Add the five ppc64le rows + N/A comments (TDD green, single committed change)

**Files:**
- Modify: `fixtures/local-libvirt/rootfs_catalog.toml`

**Interfaces:**
- Consumes: the Global-Constraints URL/sha256/version tables; the existing row format (copy the
  shape of `fedora-kdive-ready-44-ppc64le`).
- Produces: the five rows that turn Task 2's tests green.

- [ ] **Step 1: Add each ppc64le row next to its x86_64 sibling**

For each new row, insert an `[[image]]` block immediately after its x86_64 sibling, following the
existing format exactly (note the `# pragma: allowlist secret` on the `source` line). Fedora
43-cloud sibling, inserted after the `fedora-kdive-ready-43-cloud` block:
```toml
# ppc64le sibling of fedora-kdive-ready-43-cloud (fedora-secondary tree). Same Fedora 43 Generic
# cloud base, ppc64le arch; rides the rhel-family customize_via="boot" path (ADR-0345) for
# cross-arch customization under TCG. makedumpfile/drgn mirror the x86_64 sibling (Fedora ships
# arch-identical package versions). #1152, epic #1139.
[[image]]
name = "fedora-kdive-ready-43-cloud-ppc64le"
distro = "fedora"
version = "43"
family = "rhel"
arch = "ppc64le"
kind = "debug"
makedumpfile_version = "1.7.8"  # mirrors fedora-kdive-ready-43-cloud (arch-invariant)
drgn_version = "0.2.0"  # mirrors fedora-kdive-ready-43-cloud (Fedora 43 repos)
source = { kind = "cloud-image", url = "https://dl.fedoraproject.org/pub/fedora-secondary/releases/43/Cloud/ppc64le/images/Fedora-Cloud-Base-Generic-43-1.6.ppc64le.qcow2", sha256 = "50c51fb73f01722b737031c333e92c0fe5bd0c3760421fe104bd888941cd41bb" }  # pragma: allowlist secret
```
Rocky 9 sibling, after `rocky-kdive-ready-9`:
```toml
[[image]]
name = "rocky-kdive-ready-9-ppc64le"
distro = "rocky"
version = "9"
family = "rhel"
arch = "ppc64le"
kind = "debug"
makedumpfile_version = "1.7.6"  # mirrors rocky-kdive-ready-9 (bundled in kexec-tools 2.0.29)
drgn_version = "0.0.33"  # mirrors rocky-kdive-ready-9 (EPEL 9 python-drgn 0.0.33-2.el9, ppc64le)
source = { kind = "cloud-image", url = "https://dl.rockylinux.org/pub/rocky/9/images/ppc64le/Rocky-9-GenericCloud-Base-9.8-20260525.0.ppc64le.qcow2", sha256 = "811376d2a8126b0ebbf649179da8e0075a619e6208d3e14932da7f9a782f83bc" }  # pragma: allowlist secret
```
Rocky 10 sibling, after `rocky-kdive-ready-10`:
```toml
[[image]]
name = "rocky-kdive-ready-10-ppc64le"
distro = "rocky"
version = "10"
family = "rhel"
arch = "ppc64le"
kind = "debug"
makedumpfile_version = "1.7.8"  # mirrors rocky-kdive-ready-10 (separate pkg, el10)
drgn_version = "0.0.33"  # mirrors rocky-kdive-ready-10 (EPEL 10 python-drgn 0.0.33-1.el10, ppc64le)
source = { kind = "cloud-image", url = "https://dl.rockylinux.org/pub/rocky/10/images/ppc64le/Rocky-10-GenericCloud-Base-10.2-20260525.0.ppc64le.qcow2", sha256 = "06ab97a625392776028dde8ef5d663550ca6e6e162efc5993fd398f59477bd42" }  # pragma: allowlist secret
```
CentOS Stream 9 sibling, after `centos-stream-kdive-ready-9`:
```toml
[[image]]
name = "centos-stream-kdive-ready-9-ppc64le"
distro = "centos-stream"
version = "9"
family = "rhel"
arch = "ppc64le"
kind = "debug"
makedumpfile_version = "1.7.6"  # mirrors centos-stream-kdive-ready-9 (bundled in kexec-tools 2.0.29)
drgn_version = "0.0.33"  # mirrors centos-stream-kdive-ready-9 (EPEL 9 python-drgn 0.0.33-2.el9, ppc64le)
source = { kind = "cloud-image", url = "https://cloud.centos.org/centos/9-stream/ppc64le/images/CentOS-Stream-GenericCloud-9-20260622.0.ppc64le.qcow2", sha256 = "11f12407e0f19f067fd90692f66b5c0c9e75fce8503c3e1d5cfe20268b8e0ec8" }  # pragma: allowlist secret
```
CentOS Stream 10 sibling, after `centos-stream-kdive-ready-10`:
```toml
[[image]]
name = "centos-stream-kdive-ready-10-ppc64le"
distro = "centos-stream"
version = "10"
family = "rhel"
arch = "ppc64le"
kind = "debug"
makedumpfile_version = "1.7.8"  # mirrors centos-stream-kdive-ready-10 (separate pkg, el10)
drgn_version = "0.0.33"  # mirrors centos-stream-kdive-ready-10 (EPEL 10 python-drgn 0.0.33-1.el10, ppc64le)
source = { kind = "cloud-image", url = "https://cloud.centos.org/centos/10-stream/ppc64le/images/CentOS-Stream-GenericCloud-10-20260622.0.ppc64le.qcow2", sha256 = "6d1ac346df9931c54fc8e88f496427d1123f1f5018b3ce8cd5ff307b835d217b" }  # pragma: allowlist secret
```

- [ ] **Step 2: Add the N/A + scope-note comments**

After the `rocky-kdive-ready-8` block, add:
```toml
# N/A (ppc64le): Rocky 8 publishes no ppc64le port — its images/ppc64le/ tree is empty (Rocky 8 is
# x86_64 + aarch64 only). No rocky-kdive-ready-8-ppc64le row; guarded by test_rootfs_catalog.py.
```
After the last debian block (`debian-kdive-ready-13`), add:
```toml
# N/A (ppc64le, deferred to #1167): Debian publishes only the `generic`/`nocloud` ppc64el variant,
# not the `genericcloud` variant these x86_64 rows pin, and the debian family is still
# customize_via="virt_customize" — it cannot cross-arch customize-boot on an x86_64 host until the
# debian->boot migration (#1167) lands. Debian ppc64le rows are added there, not here; guarded by
# test_rootfs_catalog.py.
#
# Scope note (ppc64le): fedora-kdive-build-44 (build-host toolchain) gets no ppc64le sibling — a
# ppc64le build host only matters for compiling ppc64le kernels, a lane unproven in epic #1139;
# shipping one would be a speculative, unusable row (ADR-0350).
```

- [ ] **Step 3: Run the catalog tests, verify they PASS**

Run:
```bash
uv run python -m pytest tests/images/test_rootfs_catalog.py -q
```
Expected: PASS (all, including the new parity/per-row/N/A tests and the existing
capability-iteration tests).

- [ ] **Step 4: Lint + type the touched files**

Run: `just lint && just type`
Expected: clean (the TOML change is caught by `check toml`; no Python type surface changed).

- [ ] **Step 5: Commit the rows AND the Task 2 tests together (one green commit)**

Stage both the catalog rows (this task) and the test additions left in the working tree from Task 2,
so the committed tree is green:
```bash
git add fixtures/local-libvirt/rootfs_catalog.toml tests/images/test_rootfs_catalog.py
git commit -m "feat(1152): add ppc64le rhel-family catalog rows + N/A gaps

Five sha256-pinned ppc64le siblings (Fedora 43-cloud, Rocky 9/10, CentOS
Stream 9/10), versions mirrored from the x86_64 sibling; N/A gaps (Rocky 8,
Debian->#1167) documented and guarded by tests. ADR-0350."
```
Rationale: catalog rows and their expectation tests are one logical change; committing them
together keeps every commit green (and still bisectable — the rows and the tests that assert them
never split across commits).

---

## Task 4: Full guardrail suite (build-validation gate)

**Files:** none.

**Interfaces:**
- Consumes: Tasks 2–3 committed.
- Produces: a green `just ci`, which is the catalog/loader build-validation for all five rows.

- [ ] **Step 1: Run the full gate**

Run: `just ci`
Expected: green (lint, type, lint-shell, lint-workflows, check-mermaid, test). If
`test_no_adr_leak` or a generated-doc drift check fails, resolve before proceeding (see
`feedback-run-just-ci-before-push`).

- [ ] **Step 2: Commit any generated-doc updates** (only if `just ci` produced drift):

```bash
git add -A && git commit -m "chore(1152): regenerate docs after catalog rows"
```

---

## Task 5: Live TCG customize-boot proof (CentOS Stream 9 ppc64le)

> **Runs in the main session, not a context-free subagent.** This task needs the host's KVM/libvirt
> + `qemu-system-ppc64` and interactive console watching (`host-runs-live-vm-tests`,
> `host-libvirt-modular-daemons`). The concrete entrypoint is the `build-fs --image <name>`
> customization build — the same path #1147 drove for the Fedora rhel proof; Step 1 pins the exact
> invocation from that proof record before running.

**Files:**
- Create: `docs/design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md`

**Interfaces:**
- Consumes: the committed `centos-stream-kdive-ready-9-ppc64le` row; the host `qemu-system-ppc64`
  (TCG) proven by #1144/#1146; the `build-fs` customize-boot path (ADR-0345).
- Produces: a recorded PASS/FAIL verdict with the `hvc0` marker evidence; possibly a Task 6 trigger.

- [ ] **Step 1: Locate the build-fs customize-boot entrypoint used by #1147's rhel proof**

Read `docs/design/2026-07-13-unified-customization-boot-proof-record-1147.md` and its plan for the
exact command/harness that customize-boots an rhel row under TCG (the same path #1147 proved for
Fedora). Mirror that harness with `--image centos-stream-kdive-ready-9-ppc64le`. Per
`feedback-operator-docs-no-just`, drive it via `python -m kdive` / `scripts/*.sh`, not `just`.

- [ ] **Step 2: Run the customize boot and capture the console**

Trigger the customization build for `centos-stream-kdive-ready-9-ppc64le` on the x86_64 host.
Watch the per-build `kdive-build-<uuid>` domain's `hvc0` console.
Expected PASS signal: the `kdive-customize-ok` marker on `hvc0` and a sealed image (ADR-0345).
Expected FAIL signal: `kdive-customize-failed` marker or the TCG-scaled deadline, with the
`redacted_console_tail`.

- [ ] **Step 3: Branch on the outcome**

- **PASS** → record the verdict (below) and skip Task 6.
- **FAIL because `drgn` could not install (EPEL)** → proceed to Task 6, then re-run this step.
- **FAIL because the image does not boot under the customization machinery at all** → record the
  finding + console tail, open a follow-up issue for the boot-blocking cause, and fall back to
  `rocky-kdive-ready-9-ppc64le` (then, if that also fails to boot, a Fedora ppc64le row) as the
  non-Fedora proof, per spec Live-proof risk 1. The five catalog rows still ship.

- [ ] **Step 4: Record the proof-record doc**

Create `docs/design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md` with: date, host
(x86_64, `qemu-system-ppc64` version), the row proven, the exact command, the `hvc0` marker
observed (ok/failed + tail), the recorded installed `drgn` version from the image provenance
(reconciles spec Decision 2), and the verdict. Match the structure of
`2026-07-13-ppc64le-boot-bundle-proof-record-1146.md`. No banned doc-style words.

- [ ] **Step 5: Commit**

```bash
git add docs/design/2026-07-14-ppc64le-catalog-parity-1152-proof-record.md
git commit -m "test(live): record ppc64le catalog-parity customize-boot proof (#1152)"
```

---

## Task 6 (conditional): Enable EPEL for EL9 in the rhel customizer

Run **only** if Task 5 Step 3 hit the EPEL-drgn failure.

**Files:**
- Modify: `src/kdive/images/families/rhel.py`
- Modify: `tests/images/` — the rhel-family customizer test (locate with
  `rg -l "customize_steps|_ENABLE_EPEL" tests/`)

**Interfaces:**
- Consumes: `RhelFamily.customize_steps`, `_el_major`, `_ENABLE_EPEL_CMD` (rhel.py).
- Produces: EPEL enabled for EL8 **and** EL9 (arch-agnostic; repairs the latent x86_64 EL9 rows).

- [ ] **Step 1: Enumerate existing EL8/EL9 customizer expectations that the guard change flips**

Widening `== 8` to `<= 9` changes `customize_steps` output for **every** EL9 rhel context (the
x86_64 Rocky 9 / CentOS Stream 9 rows too). Before editing, find every test that asserts the
EL8/EL9 step list so an intended flip is not mistaken for a regression:
```bash
rg -n "epel|EPEL|_el_major|== 8|<= 9|customize_steps|_ENABLE_EPEL" tests/
```
Any test asserting that EL9 currently emits **no** EPEL step must be updated in this task's commit
(it is now expected to emit one) — update the expectation, do not weaken the assertion.

- [ ] **Step 2: Write the failing test** — assert `customize_steps` for an EL9 rhel debug context
  (distro `centos-stream`, version `9`, packages including `drgn`) contains an EPEL-enable
  `RunCommand` before the `drgn` `InstallPackages`. Mirror the existing EL8 EPEL test.

- [ ] **Step 3: Run it, verify FAIL** (`rg`-found test path, `-q`).

- [ ] **Step 4: Widen the guard** — in `rhel.py:customize_steps`, change the EL8-only guard
  `if _el_major(ctx.distro, ctx.version) == 8 and "drgn" in ctx.packages:` to fire for every EL
  major that installs `drgn` from EPEL (`major is not None and major <= 9`). If CentOS Stream 9
  needs CRB enabled before `epel-release`, extend `_ENABLE_EPEL_CMD` accordingly (record the exact
  command the live proof required — do not guess; the proof output is the source of truth).

- [ ] **Step 5: Run the rhel-family tests (incl. the Step 1 updated expectations) + the catalog
  tests, verify PASS.**

- [ ] **Step 6: `just ci`, then commit**

```bash
git add src/kdive/images/families/rhel.py tests/images/
git commit -m "fix(1152): enable EPEL for EL9 rhel customization (drgn from EPEL)"
```

Then re-run Task 5 Step 2–5.

---

## Self-review notes

- **Spec coverage:** rows (Task 3) ✓; version parity (Task 2/3) ✓; N/A documented + tested (Task
  2/3) ✓; URL-resolve (Task 1) ✓; live proof + falsifiable signal + fallback (Task 5) ✓; honest
  build-validation = loader + `just ci` + URL-resolve (Task 4) ✓; EPEL contingency (Task 6) ✓.
- **No new public contract / schema:** the loader is untouched; only data + tests + a conditional
  customizer fix.
- **Rollback:** every task is a single additive commit; reverting Task 3's commit removes the rows
  and Task 2's tests then fail loudly (expected). No migration, no external write.
