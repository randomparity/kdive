# HMC Feasibility Verification — Live Results

**Issue:** #1808 — Feasibility: direct-HMC PowerVM provider for M5 LPAR composition  
**Branch:** `feat/hmc-feasibility-verification-1808`  
**HMC version tested:** V10R3 M1060 (build 2408210051)

This document records the live-HMC verification of the open questions from #1808 that
required hardware access.  The three verification scripts in `scripts/` can be re-run
against any HMC by sourcing an `env.sh` with `HMC_HOST`, `HMC_USER`, `HMC_PASS`, and
`HMC_SYST` set.  **Do not commit `env.sh`** — it contains credentials.

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/hmc_verify_q3_latency.py` | Create a minimal LPAR, power it on, poll state and refcode; measure wall-clock time at each stage |
| `scripts/hmc_verify_q4_ssh_sessions.py` | Ramp concurrent SSH connections to find the practical session ceiling |
| `scripts/hmc_verify_q8_phb_bifurcation.py` | Enumerate PCIe slots; detect bifurcated PHBs and cross-partition assignments |

All three scripts are read-only except Q3, which creates and **immediately deletes** a
test LPAR (timestamped name, `finally`-block cleanup).  No permanent changes are made to
any frame.

**Requirements:** `sshpass` on `PATH`; Python 3.12+; stdlib only (no third-party packages).

---

## Q3 — LPAR create, power-on, and activation latencies

### Result

| Milestone | Wall-clock seconds |
|-----------|--------------------|
| `mksyscfg -r lpar` returns | **4.75 s** |
| `chsysstate -o on -f default_profile` returns | **4.09 s** |
| State = Running (first poll) | **5.62 s** after power-on command |
| First reference code (`CA00E1F1`) | **5.62 s** after power-on command |

Single run on V10R3 M1060, shared-processor LPAR, 0.1 proc units, 1024 MB, no storage or
network.  Activation reached "Running" within the first poll interval (5 s) — sub-10 s
total from power-on command to Running state.

### Implications for M5 deadline contracts

- `mksyscfg` is synchronous on the CLI and completes in under 5 s; this is not a
  long-running operation and does not need to be modelled as an async Job.
- `chsysstate -o on` also returns synchronously in under 5 s.  The state-Running
  milestone follows within one poll interval.  For a minimal LPAR on a lightly loaded
  frame, 30 s is a conservative poll timeout.
- For a realistic M5 deadline contract (fully configured LPAR with storage, network, and
  firmware self-test), the IBM REST API's 3600 s default timeout remains the worst-case
  ceiling.  A practical tight deadline is likely 120–300 s; confirm with workload-sized
  tests once storage and network profiles are included.
- The first reference code appears immediately at the same poll that sees Running state,
  confirming that `lsrefcode` does not require holding the exclusive vterm and can be used
  in parallel with state polling.

### Gotchas discovered during calibration

1. **`sharing_mode` value is `uncap`, not `uncapped`.**  The Ansible collection uses
   `uncapped`; the HMC CLI rejects it on V10R3 with "invalid attribute value".

2. **Both proc_units and procs must be specified together for shared-mode LPARs.**
   `mksyscfg` requires `min_procs`, `desired_procs`, and `max_procs` (virtual CPU count)
   alongside `min_proc_units`, `desired_proc_units`, and `max_proc_units` — omitting the
   procs triplet returns an error even when `proc_mode=shared`.

3. **`chsysstate -o on` requires `-f <profile_name>` for freshly created LPARs.**
   A new LPAR's "current configuration" has no I/O resources assigned until first
   activation; attempting to activate without `-f` returns HSCL3680 ("insufficient
   resources in its current configuration").  Use `-f default_profile` (or the named
   profile) to activate from the profile.  Subsequent activations can omit `-f` once
   the current configuration is populated.

4. **Frames can be fully proc-allocated.**  `lshwres -r proc --level sys -F
   curr_avail_sys_proc_units` returning `0.0` means no LPAR can be created on that
   frame.  The Q3 script queries all candidate frames and selects the first with
   available headroom; hard-coding a single system name will cause silent failures on
   a saturated frame.

5. **`lssyscfg -F proc_mode` and `-F sharing_mode` are not valid field names.**  Query
   the full record without `-F` and parse the key=value output, or use the REST API's
   quick endpoint if specific fields are needed.

---

## Q4 — Concurrent SSH session limit

### Result

| Concurrent sessions | Outcome |
|---------------------|---------|
| 1 – 40 | All succeeded (100% success rate) |
| 45 | First failures — `kex_exchange_identification: Connection reset by peer` |
| 50 | ~20% failure rate |

**Practical SSH concurrency ceiling: 40 sessions clean; failures begin at 45.**

The error is a TCP-level connection reset at key exchange, not an application-layer
rejection message.  This is consistent with the HMC closing the SSH listener once a
per-user or per-service connection limit is reached.

### Implications for M5 design

- The advisory-lock discipline in constraint 1 of #1808 should extend to a per-HMC
  scope; the ceiling is not high enough to treat the HMC as an unlimited resource.
- A KDIVE deployment managing multiple frames on one HMC should budget no more than
  ~30 concurrent SSH connections with comfortable headroom below the 40-session clean
  floor, accounting for operator sessions and monitoring traffic.
- The WebUI session limits (`max_webui_sessions_per_user=100`) are separate from and
  much higher than the SSH limit found here; do not conflate them.
- All connections closed cleanly on success; no session-leak accumulation was observed
  during the ramp test.  The REST session-leak hazard documented in #1808 does not apply
  to the SSH path.

---

## Q8 — PCIe PHB bifurcation across partitions

### Result

On the tested frame (Power9, V10R3 M1060):

- **15 PCIe slots** enumerated via `lshwres -r io --rsubtype slot`
- **All 15 slots have `bus_grouping=0`** — no HMC-visible bifurcated PHBs
- **All 15 slots have `parent_slot_drc_index=none`** — no child/double-wide slots
- **3 slots assigned to partitions** (2 to a VIOS, 1 to a client LPAR); 12 empty

**Finding: INCONCLUSIVE for this frame.**  The frame has no bifurcated PHBs visible
to the HMC CLI.  Cross-partition bifurcation cannot be confirmed or denied from this
dataset alone.  The frame topology does not provide a test case.

### How to close Q8

The question requires either:
- A frame where `bus_grouping != 0` or `parent_slot_drc_index != none` appears in
  `lshwres` output; the Q8 script will automatically detect and report cross-partition
  assignments if present.
- An IBM statement confirming or denying that two slots sharing a PHB (via bifurcation)
  can be assigned to different partitions.

POWER9 per-stack error isolation architecture implies PHB-level independence, which
suggests bifurcation-to-different-partitions is possible, but no IBM documentation
confirms it explicitly.

### Implications for M5 design

PCIe slot assignment for M5 should proceed using per-slot DRC indexes as specified in
`mksyscfg` and `chhwres`.  The bifurcation edge case is a future refinement: if a
double-wide adapter occupies two physical slots, both slots must be assigned to the same
partition (the constraint already stated in #1808's port coverage table).  The script
in this branch can be re-run on any frame to produce a definitive answer when a
suitable frame is available.

---

## HMC CLI surface notes (V10R3 M1060)

These are calibration findings not in the original #1808 body that are worth recording
for the M5 design.

### `lshwres` output format

`lshwres -r io --rsubtype slot` returns **key=value pairs** (e.g.
`bus_id=19,phys_loc=C11,drc_index=21020013,...`), not the CSV delimited format that
`-F` produces.  Values can themselves contain commas (e.g. `feature_codes=5260,5899`),
so parsing requires splitting on `,key=` boundaries rather than plain comma-split.

When `-F` is used to request specific fields, the output is plain comma-separated values
without keys — but not all fields available in the default output are valid `-F` field
names.  The script uses the default (full key=value) format and parses it.

### `mksyscfg` attribute name differences from Ansible collection

The `ibm.power_hmc` Ansible collection uses slightly different attribute naming
conventions and applies defaults internally.  When calling the HMC CLI directly:

| Intent | CLI attribute | Common mistake |
|--------|--------------|----------------|
| Shared processor mode | `proc_mode=shared` | `proc_mode=shared_proc` |
| Uncapped sharing | `sharing_mode=uncap` | `sharing_mode=uncapped` |
| Activate with profile | `chsysstate -o on -f <profile>` | `chsysstate -o on` (HSCL3680) |

### `lssyscfg` field availability

Several field names that appear intuitive are not valid `-F` values on this HMC version:

- `proc_mode` — not a valid `-F` field; use full record output and parse key=value
- `sharing_mode` — not a valid `-F` field
- `owner_type` — not a valid field in `lshwres -r io --rsubtype slot` output

Always test `-F field_name` against the target HMC before hardcoding it in production
code; field availability varies across HMC release levels.

---

## Running the scripts

```sh
# 1. Create env.sh (never commit this file)
cat > env.sh <<EOF
HMC_HOST=<hmc-hostname-or-ip>
HMC_USER=<username>
HMC_PASS='<password>'
HMC_SYST=<managed-system-name>   # frame name, e.g. the short name from lssyscfg -r sys -F name
HMC_LPAR=<existing-lpar-name>    # informational only; Q3 creates its own test LPAR
EOF

# 2. Load credentials
set -a && . ./env.sh && set +a

# 3. Run Q8 first (read-only, fastest)
python3 scripts/hmc_verify_q8_phb_bifurcation.py

# 4. Run Q4 (read-only, ~2 min)
python3 scripts/hmc_verify_q4_ssh_sessions.py

# 5. Run Q3 (creates+deletes one test LPAR, ~1 min on a responsive frame)
python3 scripts/hmc_verify_q3_latency.py

# Results are written to /tmp/hmc_q{3,4,8}_*.json
```

**Note on HMC_SYST:** the managed system name is the short frame name as returned by
`lssyscfg -r sys -F name`, **not** an LPAR name.  An LPAR name (e.g. `frameNN-lp1`) will
return HSCL8018.  Verify with `lssyscfg -r sys -F name` before running.
