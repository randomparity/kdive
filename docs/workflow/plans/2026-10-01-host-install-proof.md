# Host-installation proof — implementation plan (#2807)

**Goal:** produce honest `host-install/local-libvirt/x86_64/<family>` evidence from clean hosts
through a reusable runner and an on-host boot node, then run it on three families.

**Architecture:** a controller-side runner (`scripts/host_install_proof.py`) drives the
documented entry points over SSH and composes one version-1 `Evidence` per cell. An on-host
pytest node (`tests/integration/test_host_install_live.py`) proves each boot phase and writes a
phase record. See the [spec](../specs/2026-10-01-host-install-proof-design.md) and
[ADR-0716](../../adr/0716-host-install-evidence-producer.md).

**Tech stack:** Python 3.14, stdlib `subprocess`/`shlex`/`json`/`tarfile`, pydantic (already a
dependency), pytest, `uv`.

Expected implementation size: 700–1000 changed lines (L) — runner ~380, node ~260, unit tests ~250, docs/proof record ~150.

## Global Constraints

- Python 3.14; ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict, whole tree.
- No new dependency. Runner imports only stdlib, `scripts.coverage_campaign.{contract,evidence}`,
  `scripts.kernel_fixtures`, and `kdive.images.rootfs.catalog`.
- New environment variables are unprefixed (`HOST_INSTALL_*`): a `KDIVE_*` token under
  `scripts/` or `tests/` fails `env-docs-check` unless published.
- Committed text carries no host name, address, account, VMID or inventory path; use
  `lab-ubuntu`, `lab-fedora`, `lab-rocky`. Prose avoids "critical", "robust", "comprehensive",
  "elegant".
- Do not edit `scripts/coverage_campaign/{contract,evidence,results}.py` or
  `tests/integration/live_stack/*` (direct dependencies, read-only here); the
  `obligations.toml` edit is one additive `[implementations]` line.
- Guardrails: `just test-changed`, `just lint`, `just type`, `just coverage-check`,
  `git fetch origin main && just records`; full `just ci > <file> 2>&1 < /dev/null`.

## File map

| File | Change | Owns |
|---|---|---|
| `scripts/host_install_proof.py` | create | runner CLI (`run`, `merge`), step scripts, bundle cut, binding, phase parsing, evidence composition, node helper functions |
| `tests/scripts/test_host_install_proof.py` | create | unit tests for every pure function above |
| `tests/integration/test_host_install_live.py` | create | the on-host phase node |
| `scripts/coverage_campaign/obligations.toml` | modify | `"host-install"` implementation binding |
| `docs/development/coverage-qualification.md` | modify | runner usage, output contract, merge |
| `docs/operating/install.md` | modify | unattended become prompt; proven-family pointer |
| `docs/operating/providers/local-libvirt.md` | modify | proof status pointer |
| `docs/design/2026-10-01-host-install-proof-record-2807.md` | create | sanitized live record |

No ownership moves; no caller migration.

## Task 1 — Runner pure core

Files: `scripts/host_install_proof.py` (pure part), `tests/scripts/test_host_install_proof.py`.

**Interfaces (produced):**

```text
ASSERTIONS = ("clean-install", "first-boot", "repeat-setup", "second-boot", "confinement", "cleanup")
CLEAN_STEPS = ("observe-host", "operator-prerequisites", "bootstrap", "clone", "setup", "prepare",
               "preflight", "stack", "guest-image")
REPEAT_STEPS = ("repeat-setup", "repeat-prepare", "repeat-preflight", "repeat-stack")
@dataclass(frozen=True) class Step: name: str; exit_code: int; seconds: float; transcript_sha256: str
class PhaseRecord(BaseModel, extra=forbid, strict): phase: Literal["first-boot","second-boot"];
    passed: bool; deployed: dict[Role, GitSHA | None]; context: Context;
    confinement: dict[str, str]; cleanup: dict[str, bool]; failures: list[str]
def validate_target(value: str) -> str           # ^[a-z_][a-z0-9_-]*@[A-Za-z0-9.:-]+$ else ValueError
def validate_name(value: str) -> str             # catalog/image names ^[a-z0-9][a-z0-9._-]*$
def step_script(template: str, **values: str) -> str   # str.format with shlex.quote(values)
def read_phase(path: Path) -> PhaseRecord | None  # None when absent/oversize(>1 MiB)/invalid
def console_has_release(text: str, release: str) -> bool  # line contains "Linux version <release> "
def label_confined(label: str) -> bool           # svirt_t in SELinux label, or AppArmor profile not unconfined/"-"
def package_digest(root: Path) -> str            # sha256 over sorted (relpath, sha256) of *.py
def compose(*, cell: Cell, candidate: str, matrix: str, binding: Context, steps: list[Step],
            phases: list[PhaseRecord | None], host: dict[str, object], operator_sha256: str | None,
            seconds: float, artifacts: Path) -> Evidence
def merge(runs: list[Path], output: Path) -> None   # writes inputs.json, results.json
```

Borrowed and confirmed: `Cell`, `build_contract`, `digest` (`scripts/coverage_campaign/contract.py`);
`Context`, `Evidence`, `InputBindings`, `Outcome`, `Role`, `GitSHA` (`.../evidence.py`).

**Behaviour of `compose`:**
- `blocked` + `["missing-prerequisite"]` when `host["reachable"]` is false, or any of
  `host["clean"]`, `host["sudo"]`, `host["kvm"]` is false.
- Each assertion artifact is canonical JSON (`json.dumps(sort_keys=True, separators=(",",":"))`)
  written to `artifacts/<sha256>.json`; assertions map to those digests; `artifacts` is sorted.
- Holds rules exactly as the spec table; any not-holding assertion → `failure` (still
  recorded with its artifact so the record shows what ran). Absent phase → that boot, the
  confinement and the cleanup assertions do not hold.
- `deployed_roles` = roles whose value is identical, non-null, in both phases; missing role or
  value ≠ candidate → `failure`.
- `context` = phase context if both phases equal it, else `binding`, and `failure`.

**Verification:**
- `Mode: focused-test` — composition and outcomes. Cases: all-pass synthetic run qualifies
  `success` through `scripts.coverage_campaign.results.qualify` restricted to the cell; failed
  `prepare` step → `failure`; missing second phase → `failure`; worker role `None` → `failure`;
  context mismatch → `failure`; non-clean host → `blocked` with `missing-prerequisite`.
  Red: module absent (`ModuleNotFoundError`). Green:
  `uv run python -m pytest tests/scripts/test_host_install_proof.py -q`.
- `Mode: focused-test` — validation and quoting: `validate_target("a@b;rm")` raises;
  `step_script("ls {p}", p="a b'c")` yields a `shlex.split`-able single argument. Same command.
- `Mode: focused-test` — `read_phase` returns `None` for missing, oversize, extra-field and
  wrong-type files. Same command.
- `Mode: focused-test` — helpers: `console_has_release` matches only the exact release token;
  `label_confined` true for `system_u:system_r:svirt_t:s0:c1,c2` and `libvirt-<uuid> (enforce)`,
  false for `unconfined_u:unconfined_r:unconfined_t:s0`, `unconfined`, `-`; `package_digest`
  changes when one byte of a `.py` changes and ignores `__pycache__`. Same command.
- `Mode: focused-test` — `merge` raises on duplicate cell binding and on candidate mismatch.

Steps: write tests → run (red) → implement → run (green) → `just lint` → commit
`test(coverage): compose host-install evidence` style message.

## Task 2 — Runner orchestration

File: `scripts/host_install_proof.py` (`run` subcommand, `main`).

**Interfaces (consumes Task 1; produces CLI):**

```text
python -m scripts.host_install_proof run --target U@H --known-hosts F --family F --candidate SHA
    --fixture DIR [--baseline longterm] --guest-image NAME [--operator-prerequisites FILE] --output DIR
python -m scripts.host_install_proof merge --output DIR RUN_DIR...
Output DIR: binding.json (InputBindings, one cell), result.json ([Evidence]), artifacts/,
    steps/<n>-<name>.log (private), phases/<phase>/phase.json, summary.json (step table)
Exit: 0 success; 1 failure/blocked recorded; 2 invalid input (nothing written beyond DIR).
```

Behaviour:
- Preconditions (exit 2): candidate is 40-hex, equals `git rev-parse HEAD`, worktree clean;
  output absent; family is one of the contract families; cell
  `host-install/local-libvirt/<fixture arch>/<family>` exists in `build_contract()` and has a
  `node_id`; guest image row exists and its arch equals the fixture arch; fixture verifies.
- `ssh(script)` = `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=F
  -o ConnectTimeout=30 TARGET bash -l -s` with the script on stdin; transcript file written,
  exit code and seconds recorded; ssh exit 255 marks the host unreachable.
- Step scripts are module constants (fixed text with `{name}` placeholders filled through
  `step_script`). `prepare` exports
  `KDIVE_LIFECYCLE_WITNESS_DATABASE_URL` set to the live-stack runbook's documented disposable
  local witness-member DSN (`docs/operating/runbooks/live-stack.md`, "witness_dsn") and pipes `printf '\n'` to the recipe.
- Bundle: in a temp dir, `make -C <fixture> modules_install INSTALL_MOD_PATH=<tmp>/mod
  ARCH=<x86|powerpc> INSTALL_MOD_STRIP=1`, strip ppc64le boot member, `tar -czf kernel.tar.gz`
  boot member first (as `boot/vmlinuz`) then `lib/modules` excluding `*/build`, `*/source`; copy
  `.config` → `effective_config`, `manifest.json`; `kernel_sha256` = sha256 of the tar member.
- Stops at the first failing step; always writes `result.json` via `compose`.

Verification: `Mode: task-test-not-applicable` — the SSH sequence, bundle cut and copy need a
reachable clean host and a built kernel tree; no local observation could fail meaningfully
without them. Proven by the Task 5 live runs. The pure decisions it calls are Task 1's tests.

## Task 3 — On-host node

File: `tests/integration/test_host_install_live.py`.

**Interfaces (consumes):** `LiveStackClient.over_http`, `oidc_issuer_from_env`
(`kdive.mcp.dev_harness`); `ok`, `scalar`, `await_system_state`, `drain_job`, `mint_role_token`,
`build_profile`, `put_presigned`, `sha256_b64`, `full_artifact_text`
(`tests/integration/live_stack/spine.py`); `data_str` (`tests/mcp/json_data.py`); `probe_stack_skew`, `repo_facts`
(`tests/integration/live_stack/skew.py`); `arch_traits`; `load_rootfs_catalog`;
`PhaseRecord`, `console_has_release`, `label_confined`, `package_digest` (Task 1).
Each borrowed name is confirmed in the source before use; one that is absent is replaced by the
spine's equivalent at implementation time, not invented.

Behaviour: as the spec's Node section; phase JSON written before the final `assert passed`;
release in `finally` when the allocation exists and was not released.

Verification: `Mode: task-test-not-applicable` — the node's every observation is of a freshly
installed live stack; the pure decisions are Task 1's tests. Proven by Task 5. Structural:
`just coverage-check` validates the node path and function exist (Task 4).

## Task 4 — Bind and document

- `obligations.toml` `[implementations]`:
  `"host-install" = "tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel"`.
- `Mode: focused-test` — `just coverage-check` output shows pending count reduced by 3 and exit
  0; red before Task 3 exists (`invalid-contract`, exit 2).
- Docs (coverage-qualification runner section; install unattended become note; local-libvirt
  status) — `Mode: task-test-not-applicable`: prose; gated by `just docs-check docs-links docs-paths`.

## Task 5 — Live runs and proof record

Per family, sequentially (one guest at a time): lab restore to `clean` → `run` → collect output →
restore `clean`. Then `merge` + `qualify`; record each host-install cell verdict, step durations,
sanitized identities (candidate SHA, matrix, OS versions, fixture id, release, image digest) and
any defect issue (filed `status:needs-triage`, linked). `Mode: task-test-not-applicable` — the
record is the evidence itself. Commit the record docs-only after the runs.

Rollback: docs and an additive manifest line; reverting the PR removes the binding and returns
the three cells to pending.
