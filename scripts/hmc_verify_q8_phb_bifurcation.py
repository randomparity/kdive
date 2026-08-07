#!/usr/bin/env python3
"""
hmc_verify_q8_phb_bifurcation.py — Inspect the PCIe/PHB topology on a live
managed system to assess whether bifurcated PHB slots can be split across
partitions, answering open question 8 from issue #1808.

Usage:
    python scripts/hmc_verify_q8_phb_bifurcation.py

Environment (from env.sh):
    HMC_HOST  — HMC hostname or IP
    HMC_USER  — HMC username
    HMC_PASS  — HMC password
    HMC_SYST  — managed-system name (the frame name, e.g. ltczz386)

What it collects (all read-only):
    lshwres -r io --rsubtype slot — full per-slot inventory in key=value
    format.  Relevant fields:
        bus_id, drc_index, phys_loc, description, lpar_name, lpar_id,
        bus_grouping, parent_slot_drc_index, lpar_assignment_capable,
        dynamic_lpar_assignment_capable

Bifurcation evidence is assessed two ways:

    1. bus_grouping != 0 — HMC explicitly marks grouped (bifurcated) PHBs.
       Slots with the same non-zero bus_grouping value share a PHB.

    2. parent_slot_drc_index != 'none' — A slot that is a child of another
       slot is the canonical sign of double-wide / bifurcated assignment.

    For any group identified by either method, the script checks whether
    distinct lpar_name values appear within the group — direct evidence that
    cross-partition bifurcation is in use on this frame.

The script does NOT create, modify, or delete any partition or slot.
Results are printed to stdout and written to /tmp/hmc_q8_bifurcation.json.
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────

HMC_HOST = os.environ.get("HMC_HOST", "")
HMC_USER = os.environ.get("HMC_USER", "")
HMC_PASS = os.environ.get("HMC_PASS", "")
HMC_SYST = os.environ.get("HMC_SYST", "")

RESULTS_PATH = "/tmp/hmc_q8_bifurcation.json"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=15",
    "-o", "LogLevel=ERROR",   # suppress PQ-key warning noise
]


# ── SSH helpers ───────────────────────────────────────────────────────────────

def ssh_ok(cmd: str) -> str:
    """Run HMC CLI command, return stdout. Raises RuntimeError on failure."""
    env = os.environ.copy()
    env["SSHPASS"] = HMC_PASS
    result = subprocess.run(
        ["sshpass", "-e", "ssh"] + SSH_OPTS + [f"{HMC_USER}@{HMC_HOST}", cmd],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"HMC command failed (rc={result.returncode})\n"
            f"  cmd: {cmd}\n"
            f"  stderr: {result.stderr.strip()}\n"
            f"  stdout: {result.stdout.strip()}"
        )
    return result.stdout.strip()


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_kv_rows(raw: str) -> list[dict]:
    """Parse lshwres key=value output.  Each line is one slot record.

    Values may themselves contain commas (e.g. feature_codes=5260,5899),
    so we split on ',key=' boundaries using a regex rather than plain split.
    """
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "No results" in line:
            continue
        # Split on commas that are immediately followed by a word= pattern
        tokens = re.split(r",(?=[a-zA-Z_]+=)", line)
        row: dict = {}
        for token in tokens:
            if "=" in token:
                k, _, v = token.partition("=")
                row[k.strip()] = v.strip()
        if row:
            rows.append(row)
    return rows


# ── Analysis ──────────────────────────────────────────────────────────────────

def find_bifurcation_groups(slots: list[dict]) -> list[dict]:
    """
    Identify candidate bifurcated groups using two indicators:
      1. bus_grouping field is non-zero — HMC marks grouped PHBs explicitly.
      2. parent_slot_drc_index is not 'none' — double-wide/child slot.

    Returns one dict per group with membership and cross-partition finding.
    """
    groups: dict[str, list[dict]] = defaultdict(list)

    # Method 1: bus_grouping value (group key = "bg:<value>")
    for slot in slots:
        bg = slot.get("bus_grouping", "0")
        if bg not in ("0", "", "none"):
            groups[f"bg:{bg}"].append(slot)

    # Method 2: parent_slot_drc_index links (group key = "parent:<drc>")
    drc_to_slot = {s.get("drc_index", ""): s for s in slots if s.get("drc_index")}
    for slot in slots:
        parent = slot.get("parent_slot_drc_index", "none")
        if parent and parent != "none":
            parent_slot = drc_to_slot.get(parent)
            key = f"parent:{parent}"
            groups[key].append(slot)
            if parent_slot and parent_slot not in groups[key]:
                groups[key].append(parent_slot)

    results = []
    for group_key, members in sorted(groups.items()):
        # Deduplicate by drc_index
        seen: set[str] = set()
        unique = []
        for m in members:
            drc = m.get("drc_index", "")
            if drc not in seen:
                seen.add(drc)
                unique.append(m)

        owners = {
            m.get("lpar_name", "").strip()
            for m in unique
            if m.get("lpar_name", "").strip() not in ("", "none")
        }
        split = len(owners) > 1
        results.append({
            "group_key": group_key,
            "member_count": len(unique),
            "members": [
                {
                    "drc_index": m.get("drc_index", ""),
                    "phys_loc": m.get("phys_loc", ""),
                    "bus_id": m.get("bus_id", ""),
                    "description": m.get("description", ""),
                    "lpar_name": m.get("lpar_name", ""),
                    "lpar_assignment_capable": m.get("lpar_assignment_capable", ""),
                    "dynamic_lpar_assignment_capable": m.get("dynamic_lpar_assignment_capable", ""),
                }
                for m in unique
            ],
            "distinct_owners": sorted(owners),
            "split_across_partitions": split,
        })
    return results


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(system: str, all_slots: list[dict], groups: list[dict]) -> None:
    print(f"\n=== PCIe Slot Inventory: {system} ===")
    print(f"  Total slots: {len(all_slots)}")
    assigned = [s for s in all_slots if s.get("lpar_name", "").strip() not in ("", "none")]
    print(f"  Assigned to a partition: {len(assigned)}")
    empty = len(all_slots) - len(assigned)
    print(f"  Empty / unassigned: {empty}")

    print(f"\n=== PHB Bifurcation Analysis ===")
    if not groups:
        print("  No bifurcated/grouped slots found on this system.")
        print("  Q8 FINDING: This frame has no PHB bifurcation visible to the HMC CLI.")
        print("  Cannot confirm cross-partition bifurcation assignment from this dataset.")
    else:
        print(f"  Bifurcation groups found: {len(groups)}")
        for g in groups:
            split_note = (
                "  ← SPLIT ACROSS PARTITIONS"
                if g["split_across_partitions"]
                else ""
            )
            print(f"\n  Group [{g['group_key']}]  members={g['member_count']}{split_note}")
            for m in g["members"]:
                print(
                    f"    drc={m['drc_index']:12s}"
                    f"  bus={m['bus_id']:4s}"
                    f"  phys={m['phys_loc']:10s}"
                    f"  lpar={m['lpar_name'] or '(none)':25s}"
                    f"  dlpar={m['dynamic_lpar_assignment_capable']}"
                    f"  {m['description'][:40]}"
                )
            print(f"    Distinct owners: {g['distinct_owners'] or ['(none)']}")

        split_count = sum(1 for g in groups if g["split_across_partitions"])
        print(f"\n=== Q8 Finding ===")
        if split_count > 0:
            print(f"  CONFIRMED: {split_count} group(s) have slots owned by distinct partitions.")
            print(f"  Cross-partition PHB bifurcation IS in use on this frame.")
        else:
            print(f"  INCONCLUSIVE: Bifurcated/grouped PHBs exist but all slots share one owner.")
            print(f"  Cannot confirm cross-partition assignment from current layout alone.")
            print(f"  A live assignment test or IBM statement is needed to close Q8.")


# ── Entry point ───────────────────────────────────────────────────────────────

def _check_env() -> None:
    missing = [v for v in ("HMC_HOST", "HMC_USER", "HMC_PASS", "HMC_SYST") if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}\nSource env.sh first.")


def main() -> None:
    _check_env()

    print("=== HMC Q8 PCIe/PHB Bifurcation Probe ===", flush=True)
    print(f"    HMC   : {HMC_HOST}", flush=True)
    print(f"    System: {HMC_SYST}", flush=True)
    print(f"    Time  : {datetime.now(timezone.utc).isoformat()}", flush=True)

    try:
        print(f"\n[*] Fetching slot inventory for {HMC_SYST}...", flush=True)
        raw = ssh_ok(f"lshwres -r io --rsubtype slot -m '{HMC_SYST}'")
        all_slots = parse_kv_rows(raw)
        print(f"[+] {len(all_slots)} slot record(s) retrieved.", flush=True)

        groups = find_bifurcation_groups(all_slots)
        print_report(HMC_SYST, all_slots, groups)

        output = {
            "hmc_host": HMC_HOST,
            "system": HMC_SYST,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "total_slots": len(all_slots),
            "bifurcation_groups": len(groups),
            "split_group_count": sum(1 for g in groups if g["split_across_partitions"]),
            "groups": groups,
            "all_slots": all_slots,
        }
        with open(RESULTS_PATH, "w") as fh:
            json.dump(output, fh, indent=2)
        print(f"\n[+] Full results written to {RESULTS_PATH}", flush=True)

    except Exception as exc:
        print(f"\n[!] Error: {exc}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
