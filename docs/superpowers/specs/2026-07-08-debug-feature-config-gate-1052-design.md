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
registry: `src/kdive/kernel_config/requirements.py`. Each feature maps to an **ordered
list of requirement clauses**; a clause is an OR-group (`frozenset[str]`) satisfied when
*any* symbol in it is enabled, and a feature is supported when *every* clause is satisfied.
Plain symbols are single-element clauses; `debuginfo` uses a multi-member clause
(`DWARF5` OR `DWARF4` OR `BTF`). This is the single source of truth for both the advertised
manifest and the gate.

Registry (feature id → clauses), preserved from spec 1:

| feature id | clauses (each `{...}` is an OR-group) | gated? |
|---|---|---|
| `rootfs_mount` | `SQUASHFS`, `SQUASHFS_ZSTD`, `OVERLAY_FS`, `BLK_DEV_LOOP`, `XFS_FS`, `XFS_POSIX_ACL` | advertise only |
| `crash_capture` | `KEXEC`, `KEXEC_CORE`, `KEXEC_FILE`, `CRASH_DUMP`, `VMCORE_INFO`, `PROC_VMCORE`, `FW_CFG_SYSFS`, `RELOCATABLE`, `RANDOMIZE_BASE` | **gated** (kdump install + kdump vmcore) |
| `ikconfig` | `IKCONFIG`, `IKCONFIG_PROC` | advertise only |
| `debuginfo` | `DEBUG_INFO`, `{DEBUG_INFO_DWARF5, DEBUG_INFO_DWARF4, DEBUG_INFO_BTF}`, `DEBUG_KERNEL` | advertise only |
| `sysrq` | `MAGIC_SYSRQ` | **gated** (sysrq diagnostic) |
| `kasan` | `KASAN`, `KASAN_INLINE` | advertise only |
| `serial_console` | `SERIAL_8250_CONSOLE`, `VIRTIO_BLK`, `VIRTIO_PCI` | advertise only |

Each feature also carries a short human `summary` and a `gated` boolean for the manifest.

### 2. A read-only MCP method advertises the manifest

`catalog.feature_config_requirements` — a **static** reference tool (no arguments, no
run/system scope), returning the full feature→clause manifest with per-feature summary and
`gated` flag. It belongs in `catalog` (static reference data, beside `images.*` /
`fixtures.*` / `availability`), and is cross-referenced from the build-a-kernel journey:
`runs.create` and `artifacts.expected_uploads` add it to `suggested_next_actions` so the
agent finds it before building. The response is advisory and names no ADRs.

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
  and `feature_supported(config, feature) -> bool` over the registry.
- `src/kdive/kernel_config/fetch.py` — `load_effective_config(conn, store, run_id) ->
  KernelConfig | None`. Looks up the Run's `effective_config` artifact row
  (`owner_kind='runs'`, `owner_id=run_id`, `object_key LIKE '%/effective_config'`,
  `sensitivity=SENSITIVE`) to get `(object_key, etag)`, then `store.get_artifact` → parse.
  **Returns `None` when no row exists** (the common, optional-upload case). Tenant-agnostic:
  it reads the stored key rather than reconstructing it. The bytes are `SENSITIVE` and never
  echoed into a response — only booleans/symbol names derive from them.

### 4. Gate three seams; refuse loudly when provably unsupported

Only the config-dependent seams are gated. The gate fires **only when a config is present
and a required clause is provably unmet**; absent config → arm as today (see Decision 5).

| seam | file | feature | when it gates | refusal |
|---|---|---|---|---|
| kdump crashkernel reservation | `jobs/handlers/runs/install.py` | `crash_capture` | only when `crashkernel` was requested (kdump path) | `CategorizedError(CONFIGURATION_ERROR, reason=kernel_missing_crash_config, missing=[...])` before baking the cmdline |
| kdump vmcore fetch | `mcp/tools/lifecycle/vmcore_handlers.py` | `crash_capture` | only when resolved capture method is **KDUMP** (host_dump is host-side, needs no guest config) | `ToolResponse` failure, `CONFIGURATION_ERROR`, names the missing symbols |
| sysrq diagnostic | `jobs/handlers/diagnostic_sysrq.py` | `sysrq` | before injecting the keystroke | `CategorizedError(CONFIGURATION_ERROR, reason=kernel_missing_sysrq_config, missing=[MAGIC_SYSRQ])` — complements the existing runtime `disabled`-marker check |

Refusals name the missing `CONFIG_*` symbols and a remediation, following the existing
sysrq `CategorizedError(details={reason, remediation})` shape. "Refuse loudly" over "silently
disable" is chosen for all three because each is an **explicit agent action** (the agent
asked for crash capture / a vmcore / a sysrq) — a silent no-op would read as success.

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
- **New tool** `catalog.feature_config_requirements` (`mcp/tools/catalog/`), registered in
  the catalog registrar; keyword entry in `mcp/tool_index.py`; `suggested_next_actions`
  additions on `runs.create` + `artifacts.expected_uploads`.
- **Gate wiring** in the three seam handlers (each already has DB access; each fetches via
  its store — `diagnostic_sysrq` has `artifact_store` injected, `install`/`vmcore` build
  `object_store_from_env()`).
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
- **Fetch** — returns `None` when no artifact row; parses bytes when present (mock store).
- **Each gated seam** — three cases: (a) no config → arms as today; (b) config present &
  supported → arms; (c) config present & unsupported → refused with `CONFIGURATION_ERROR`
  naming the missing symbols. For vmcore: host_dump path never gates even with an
  unsupported config.
- Guardrails green (`just lint`, `just type`, `just test`, docs regen).
- Live smoke (plan-time, not CI-gated): upload a `.config` lacking `MAGIC_SYSRQ` → sysrq
  refused; upload one with it → sysrq works.

## Risks

- **Reading a `SENSITIVE` artifact server-side.** The bytes are fetched only at the worker/
  tool boundary and never returned; only derived booleans/symbol names leave the seam.
  Mitigation: `fetch.py` returns a `KernelConfig`, not the raw bytes, and refusal details
  list only `CONFIG_*` names (public knowledge), never config contents.
- **Over-refusal.** A wrong parse could refuse a supported kernel. Mitigation: lenient
  `=y`/`=m`, absent-config arms-as-today, and gate-only-on-present-config keep false
  refusals bounded; the parser is table-tested against real `.config` fragments.
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
