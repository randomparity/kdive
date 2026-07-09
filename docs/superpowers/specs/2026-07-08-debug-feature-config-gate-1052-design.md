# Advertise debug-feature kernel-config requirements; arm only supported features — design (Spec 3 of 3)

- **Status:** Draft
- **Date:** 2026-07-08
- **Issue:** #1052
- **ADR:** [0318](../../adr/0318-debug-feature-config-gate.md)
- **Scope:** Spec 3 of the three-spec build/config redesign
  ([spec 1](2026-07-08-remove-server-build-lane-design.md),
  [spec 2](2026-07-08-image-kernel-config-offer-1051-design.md)). Specs 1 and 2 are
  merged (PR#1056/ADR-0316, PR#1057/ADR-0317).

## Context

The redesign made **agent-builds-locally + upload-only** the sole kernel lane. The agent
builds its own kernel and uploads the artifacts; kdive installs and boots them. Spec 1
deleted all `.config` validation but deliberately **kept `effective_config` (the agent's
`.config`) as an accepted-but-unread upload** — the hook this spec uses.

kdive has **no runtime knowledge** of the booted kernel's config today. Feature gating is
done via profile bits + live probes (gdbstub reachability; the sysrq `disabled` marker,
#952), never by reading the kernel's actual `CONFIG_*`. Two gaps follow:

1. The agent is not told which `CONFIG_*` each debug feature needs, so it can build a
   kernel that silently cannot kdump / sysrq / produce a vmcore.
2. kdive arms those features regardless, so the failure surfaces late and opaquely (a
   crash that never produces a dump, a sysrq that does nothing).

## Requirements addressed

> **R1.** Every debug/platform feature that requires a kernel config setting **advertises**
> its required `CONFIG_*` symbols to the agent — advisory only; the agent decides whether
> to build them in and is free to skip any.
>
> **R2.** kdive **disables** (refuses to arm) any config-dependent feature whose required
> kernel config the agent did **not** enable in their uploaded kernel — with a clear,
> categorized reason. Advertisement stays advisory: an agent that skips a feature has
> everything else still work.

## Decisions

### 1. One feature → `CONFIG_*` registry, in a new `kernel_config` package

The feature→symbol knowledge preserved in the spec-1 doc becomes a single declarative
registry: `src/kdive/kernel_config/requirements.py`. Each feature carries **two** clause
lists:

- **`advertised`** — the full recommended superset shown by the manifest tool (guidance).
- **`gate_required`** — the minimal hard-required subset the gate refuses on (`()` = not
  gated). This is **deliberately narrower** than `advertised`: the advertise-set is
  "everything worth building for this feature," the gate-set is "the kernel provably cannot
  do this without these." Conflating them over-refuses working kernels (see below).

A clause is an OR-group (`frozenset[str]`) satisfied when *any* symbol in it is enabled; a
clause list is satisfied when *every* clause is. Plain symbols are single-element clauses.
This is the single source of truth for both the manifest and the gate.

Registry, preserved/derived from spec 1 (`{...}` = OR-group):

| feature id | `advertised` | `gate_required` |
|---|---|---|
| `rootfs_mount` | `SQUASHFS`, `SQUASHFS_ZSTD`, `OVERLAY_FS`, `BLK_DEV_LOOP`, `XFS_FS`, `XFS_POSIX_ACL` | — (advertise only) |
| `crash_capture` | `KEXEC`, `KEXEC_CORE`, `KEXEC_FILE`, `CRASH_DUMP`, `VMCORE_INFO`, `PROC_VMCORE`, `FW_CFG_SYSFS`, `RELOCATABLE`, `RANDOMIZE_BASE` | `KEXEC_CORE`, `{KEXEC, KEXEC_FILE}`, `CRASH_DUMP`, `PROC_VMCORE`, `VMCORE_INFO`, `FW_CFG_SYSFS`, `RELOCATABLE` |
| `ikconfig` | `IKCONFIG`, `IKCONFIG_PROC` | — (advertise only) |
| `debuginfo` | `DEBUG_INFO`, `{DEBUG_INFO_DWARF5, DEBUG_INFO_DWARF4, DEBUG_INFO_BTF}`, `DEBUG_KERNEL` | — (advertise only) |
| `sysrq` | `MAGIC_SYSRQ` | `MAGIC_SYSRQ` |
| `kasan` | `KASAN`, `KASAN_INLINE` | — (advertise only) |
| `serial_console` | `SERIAL_8250_CONSOLE`, `VIRTIO_BLK`, `VIRTIO_PCI` | — (advertise only) |

**Why `gate_required` ⊂ `advertised` matters (Finding 1).** `RANDOMIZE_BASE` (KASLR) is
routinely *disabled* on debugging kernels for predictable symbol addresses, and kdump works
with KASLR off (makedumpfile resolves it via vmcoreinfo). Gating on the advertised superset
would refuse exactly the debug kernels this tool serves. So `RANDOMIZE_BASE` is advertised
but **not** in the gate predicate. The two kexec load syscalls (`KEXEC` = `kexec_load`,
`KEXEC_FILE` = `kexec_file_load`) are modeled as an **OR-group** in the gate — either load
mechanism suffices; requiring both would over-refuse a kernel that ships only one.

Each feature also carries a short human `summary` and a `gated` boolean (derived: `gated ==
bool(gate_required)`) for the manifest.

### 2. A read-only MCP method advertises the manifest

`artifacts.feature_config_requirements` — a **static, read-only, auth-only** reference tool
in the **existing `artifacts` namespace** (the direct sibling of `artifacts.expected_uploads`,
which is the same static/auth-only shape, ADR-0117). No arguments, no run/system scope; it
returns the full feature manifest with the per-feature `advertised` requirements, `summary`,
and `gated` flag. The auth-only posture (a valid token, no project/RBAC gate — matching
`expected_uploads`) guarantees the tool is visible to every authenticated role that reaches
`runs.create` / `artifacts.expected_uploads`, so the cross-referenced `suggested_next_actions`
never point at an invisible tool (Finding 4). Living in `artifacts` avoids minting a new tool
namespace (`tool_index.NAMESPACE_TOC` + its completeness guard). The manifest exposes
`advertised` (guidance), **not** `gate_required` — the agent needs the full recommended set,
and the internal gate subset is an implementation detail. It pairs with `expected_uploads`
(what to upload → what config the uploaded kernel needs), so the agent finds it before
building. The response is advisory and names no ADRs.

`data` shape (one entry per feature):
`{feature, summary, gated, requirements: [[symbol, ...], ...]}` where each inner list is an
OR-group. The agent reads this and its own `.config` locally; no per-run check is offered
(the agent already holds the config it built — a server-side "will my config work" echo
would only duplicate what it can compute).

### 3. A pure parser + a read helper for the uploaded `.config`

- `src/kdive/kernel_config/parse.py` — `parse_kernel_config(data: bytes) -> KernelConfig`,
  pure. A symbol is **enabled** when the file has `CONFIG_X=y` or `CONFIG_X=m` (a loadable
  module counts as present); **disabled** on `# CONFIG_X is not set` or absence. `=y`/`=m`
  leniency matches the advisory intent. `KernelConfig` is a frozen wrapper over the enabled
  set with `is_enabled(symbol)`.
- `src/kdive/kernel_config/support.py` — pure `unmet_clauses(config, feature) -> tuple[...]`
  and `feature_supported(config, feature) -> bool` over the registry, keyed on the feature's
  **`gate_required`** clauses (a feature with empty `gate_required` is always supported).
- `src/kdive/kernel_config/fetch.py` — `load_effective_config(conn, store, run_id) ->
  KernelConfig | None`. Looks up the Run's `effective_config` artifact row
  (`owner_kind='runs'`, `owner_id=run_id`, `object_key LIKE '%/effective_config'`,
  `sensitivity=SENSITIVE`) to get `(object_key, etag)`, then `store.get_artifact` → parse.
  Tenant-agnostic: it reads the stored key rather than reconstructing it. The bytes are
  `SENSITIVE` and never echoed into a response — only booleans/symbol names derive from them.

  **The gate fails OPEN (Findings 2, 3).** `load_effective_config` returns `None` — meaning
  "cannot check, arm as today" — in every one of these cases, never propagating an error into
  the arming action:
  - no artifact row (the common, optional-upload case);
  - the object store is unconfigured or unreachable, or `get_artifact` raises (a benign
    advisory read must never convert into an install / vmcore / sysrq failure);
  - the fetched bytes parse to a **degenerate** config — **zero enabled symbols** — which
    signals a truncated / empty / wrong-file upload rather than a real `.config` (a real
    config has thousands of `=y`/`=m` lines). Treating a degenerate config as authoritative
    would refuse every gated feature on a working kernel.

  These failures are logged (warning) but do not gate. The gate refuses **only** on a
  successfully-read, non-degenerate config whose `gate_required` clauses are provably unmet.
  kdive does not (and per the no-validation rule cannot) verify that the uploaded `.config`
  corresponds to the uploaded `kernel`; the fail-open bias keeps a stale/mismatched config
  from blocking a working kernel.

### 4. Gate two Run-addressed seams with the config pre-check

The two **Run-addressed** config-dependent seams get a config pre-check: the uploaded
`effective_config` is Run-owned, so the gate reads it directly from the Run in scope. The gate
fires **only when a config is present and a required clause is provably unmet**; absent config
→ arm as today (Decision 6).

| seam | file | feature | when it gates | refusal |
|---|---|---|---|---|
| kdump crashkernel reservation | `jobs/handlers/runs/install.py` | `crash_capture` | only when `crashkernel` was requested (kdump path) | `CategorizedError(CONFIGURATION_ERROR, reason=kernel_missing_crash_config, missing=[...])` before baking the cmdline |
| kdump vmcore fetch | `mcp/tools/lifecycle/vmcore_handlers.py` | `crash_capture` | only when resolved capture method is **KDUMP** (host_dump is host-side, needs no guest config) | `ToolResponse` failure, `CONFIGURATION_ERROR`, names the missing symbols |

Refusals name the missing `CONFIG_*` symbols and a remediation, following the existing
sysrq `CategorizedError(details={reason, remediation})` shape. "Refuse loudly" over "silently
disable" is chosen because each is an **explicit agent action** (the agent asked for crash
capture / a vmcore) — a silent no-op would read as success.

### 4a. sysrq is System-addressed → runtime-gated, not pre-gated

`sysrq` is advertised (`MAGIC_SYSRQ`) but **not** given a config pre-check. The
`diagnostic_sysrq` job is **System-addressed** (`SysRqPayload` carries `system_id`, no
`run_id`), and there is **no first-class link** from a System to the Run whose kernel is
currently booted (`System` has no installed-kernel/current-Run field). A best-effort
"most-recent Run" lookup would risk a **false refusal** — a stale Run's config could lack
`MAGIC_SYSRQ` while the actually-booted kernel has it — which is worse than not pre-gating.

sysrq's config support is instead enforced by its **existing runtime detection**: a kernel
built without `MAGIC_SYSRQ` swallows the injected keystroke, producing no console delta, and
`diagnostic_sysrq_handler` already raises `CONFIGURATION_ERROR` (reason `no_console_output`).
This spec **enriches that remediation** to name `MAGIC_SYSRQ` among the causes (alongside the
existing PS/2-keyboard-driver and `kernel.sysrq` guidance), so R2's "clear, categorized
reason" is met for sysrq without a fragile mapping. R1 is met by the manifest. This parallels
the gdbstub exclusion (Decision 5): a seam the issue named, gated by the mechanism that can
actually see the condition rather than by a fetch that cannot.

### 5. gdbstub is **not** gated (excluded by design)

The QEMU gdbstub is a `<qemu:commandline>` `-gdb` passthrough (ADR-0210) that attaches to
vCPU state **regardless of the guest kernel's config**, and it is armed at **System-provision
time — before any kernel is uploaded**, from a seam
(`providers/local_libvirt/lifecycle/provisioning.py` / `xml.py`) that has neither a DB
connection nor the object store. Gating it on the `.config` is both semantically wrong (the
raw stub works without any config; only *symbol resolution* wants `DEBUG_INFO`) and
ordering-impossible (no config exists yet). So gdbstub is **advertised** via the `debuginfo`
feature (so the agent knows symbolic debugging needs `DEBUG_INFO`) but is **never disabled**.
This is a deliberate deviation from the issue's literal four-seam list, confirmed with the
operator.

### 6. Absent `effective_config` → arm as today

`effective_config` is an optional upload and is commonly absent (only `kernel` is required
by `complete_build`). When no config is uploaded, kdive cannot prove the kernel lacks a
feature, so it **arms exactly as it does today** — no behavior change for the common case.
R2 ("disable features the agent did not enable") applies only when a config exists to read.
This is backward-compatible and matches the advisory framing; the alternative (disable
everything when no config) would break every current kdump/sysrq/vmcore flow.

## What changes (files)

- **New** `src/kdive/kernel_config/{__init__,requirements,parse,support,fetch}.py`.
- **New tool** `artifacts.feature_config_requirements` — logic in
  `mcp/tools/catalog/artifacts/feature_requirements.py`, registered in
  `mcp/tools/catalog/artifacts/registrar.py` beside `expected_uploads` (no new namespace, so
  no `tool_index.NAMESPACE_TOC` change); `suggested_next_actions` additions on `runs.create`
  + `artifacts.expected_uploads`.
- **Gate wiring** in the two Run-addressed seam handlers (`install`, `vmcore`) — each has a
  DB `conn` in scope and reads the config via `object_store_from_env()`; the gate lands after
  each seam's existing preconditions and before the arming action.
- **sysrq remediation** — enrich the `no_console_output` `CategorizedError` remediation in
  `jobs/handlers/diagnostic_sysrq.py` to name `MAGIC_SYSRQ` (no config fetch).
- **Docs:** regenerate the MCP tool reference; ADR-0318 + README row.

## No schema change

The gate reads the existing `effective_config` artifact row + object; no new column or
migration. (Contrast spec 2, which added `image_catalog.kernel_config_key`.)

## Testing strategy

- **Parser** (`parse.py`) — property/table tests: `=y`/`=m` enabled; `# ... is not set`
  and absence disabled; comments/blank lines ignored; malformed lines ignored; non-UTF-8
  bytes tolerated.
- **Support** (`support.py`) — OR-group satisfied by any member; feature unsupported when
  one clause unmet; `unmet_clauses` reports exactly the missing groups.
- **Manifest tool** — returns every registry feature with `gated`/`summary`; static
  (no args); advisory (no ADR strings); envelope valid.
- **Fetch fail-open** — returns `None` (arm-as-today) for: no artifact row; store error /
  `get_artifact` raising; and a degenerate (zero-enabled-symbol) config. Parses bytes when a
  real config is present (mock store).
- **Gate predicate** — a `crash_capture` config with `RANDOMIZE_BASE` off but the
  `gate_required` set present is **supported** (KASLR-off regression guard); a config with
  only `KEXEC` (not `KEXEC_FILE`) satisfies the kexec OR-group.
- **Each Run-addressed gated seam** (`install`, `vmcore`) — three cases: (a) no config → arms
  as today; (b) config present & supported → arms; (c) config present & `gate_required`
  provably unmet → refused with `CONFIGURATION_ERROR` naming the missing symbols. For vmcore:
  host_dump path never gates even with an unsupported config.
- **sysrq remediation** — the existing `no_console_output` refusal names `MAGIC_SYSRQ` (assert
  the remediation string mentions it); no new pre-gate test (unchanged control flow).
- Guardrails green (`just lint`, `just type`, `just test`, docs regen).
- Live smoke (plan-time, not CI-gated): upload a `.config` lacking the crash symbols →
  `runs.install` with a crashkernel is refused; upload one with them → install proceeds.

## Risks

- **Reading a `SENSITIVE` artifact server-side.** The bytes are fetched only at the worker/
  tool boundary and never returned; only derived booleans/symbol names leave the seam.
  Mitigation: `fetch.py` returns a `KernelConfig`, not the raw bytes, and refusal details
  list only `CONFIG_*` names (public knowledge), never config contents.
- **Over-refusal.** A wrong parse, a KASLR-off debug kernel, or a stale/empty/mismatched
  upload could refuse a supported kernel. Mitigations, layered: the gate keys on the minimal
  `gate_required` subset (not the advertised superset), so KASLR-off kernels pass;
  `RANDOMIZE_BASE` is advertised-only and the kexec load syscalls are an OR-group; lenient
  `=y`/`=m`; absent-config and store/parse errors arm-as-today; a zero-enabled-symbol
  (degenerate) config is treated as non-authoritative rather than refusing everything; the
  parser is table-tested against real `.config` fragments.
- **Registry drift from reality.** The symbol lists are advisory and preserved from spec 1;
  an over-strict list only advertises extra symbols (harmless) or over-refuses a feature
  the agent explicitly requested (surfaced by the named-symbol reason, agent can adjust).

## Considered & rejected

- **Gate gdbstub literally** — semantically wrong + ordering-impossible (Decision 5).
- **Disable all features when no config** — breaks every current flow (Decision 6).
- **A run-scoped "will my config work" tool** — duplicates what the agent computes from its
  own `.config`; adds a SENSITIVE read to a response path for no new information.
- **A new schema column caching supported-features** — the object-store read is cheap and
  the config is per-run; a cache adds a migration and a staleness surface for no gain.
- **Silently disabling instead of refusing** — an explicit agent action that silently
  no-ops reads as success; loud, categorized refusal is more diagnosable.
- **Gating on the advertised superset (one clause list per feature)** — refuses working
  kernels: a KASLR-off debug kernel lacks `RANDOMIZE_BASE`, a kernel with only one kexec
  load syscall lacks the other. The gate must key on a minimal `gate_required` subset, so
  the registry carries `advertised` and `gate_required` separately.
- **Failing the arming action on a config-read error / degenerate config** — turns a benign
  advisory read into a new install/vmcore/sysrq failure surface; the gate fails open instead.
