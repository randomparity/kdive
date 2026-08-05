# 0544 — One clause model: settable symbols, a built-in requirement, an arch scope

## Status

Proposed

## Context

`Clause = frozenset[str]` in `src/kdive/kernel_config/requirements.py` is the whole vocabulary the
feature registry has. A clause is an OR-group of bare symbol names, satisfied when any member is in
the parsed config's `enabled` set. `FeatureRequirement` pairs an `advertised` clause tuple with a
narrower `gate_required` tuple ([ADR-0318](0318-debug-feature-config-gate.md)), and every consumer
flattens the unmet clauses into one sorted symbol union (`support.missing_symbols`) that the seams
place in the `{reason, missing, remediation}` payload
[ADR-0330](0330-complete-build-missing-boot-config-warning.md) fixed. Neither record says anything
about whether a symbol is settable, whether `=m` counts, or which arch a symbol applies to.

Four open issues each need the clause to say one of those things, and all four land in that one
file:

- **#1854** — `crash_capture.gate_required` names `KEXEC_CORE` and `VMCORE_INFO`. Both are
  prompt-less bools (`kernel/Kconfig.kexec:11` and `:8`) that `make olddefconfig` discards, so a
  refusal hands the agent two symbols and the instruction "rebuild the kernel with the missing
  `CONFIG_*`", which cannot be followed.
- **#1855** — the `debuginfo` OR-group omits `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT`, which `select`s
  `DEBUG_INFO` and produces real DWARF, so a toolchain-default kernel is reported as missing all
  three advertised symbols. It also offers `DEBUG_INFO_BTF` as an alternative, although BTF sits
  inside `if DEBUG_INFO` and selects nothing, so setting it alone on a bare config is dropped.
- **#1859** — `serial_console` advertises `SERIAL_8250_CONSOLE`, which `depends on SERIAL_8250=y`
  and is discarded unless `SERIAL_8250` is itself built in, and it advertises nothing at all for
  `ppc64le`, whose console is `hvc0` driven by `HVC_CONSOLE` (`domain/platform/arch_traits.py`).
- **#1860** — `parse.py` counts `=y` and `=m` alike, so `EXT4_FS=m` / `VIRTIO_BLK=m` /
  `VIRTIO_PCI=m` satisfy boot clauses that a direct-kernel boot with no initramfs cannot load a
  module for. A blanket regex change would be a regression: for KASAN, ftrace, kcov and BPF
  symbols, `=m` is still the feature the agent asked for.

Read together, two of the four are one defect — a clause naming a symbol the agent cannot set —
and only two of them need the type to carry anything new.

## Decision

### 1. Every clause member is a settable symbol; the prerequisite gets its own clause

A clause may name only symbols an agent can write into a fragment and see survive
`olddefconfig`. A prompt-less or auto-`select`ed symbol is never a clause member, in `advertised`
or in `gate_required`. Where such a symbol is the real requirement, the clause that names its
settable selector or dependency stands in its place, AND-ed alongside the others.

This settles #1854 without adding a field. `crash_capture.gate_required` drops `{KEXEC_CORE}` and
`{VMCORE_INFO}`; the sibling clauses `{KEXEC, KEXEC_FILE}` and `{CRASH_DUMP}` already sit in the
same refusal set, and because `KEXEC`/`KEXEC_FILE` `select KEXEC_CORE` and `CRASH_DUMP` `select`s
`VMCORE_INFO`, no config `olddefconfig` can produce lacks a derived symbol while its selector is
present. The two removed clauses can therefore only fire on an internally inconsistent upload — a
truncated or hand-edited file — where refusing is exactly the false refusal ADR-0318's fail-open
boundary exists to avoid. The refusal for a kernel that genuinely cannot kexec is unchanged, and it
now names only symbols a fragment can set, which makes ADR-0330's existing remediation sentence
true rather than requiring a new one.

The same rule applies to `bpf_tracing`, whose `{DEBUG_INFO_BTF}` clause is unreachable from a bare
config for the reason #1855 gives: BTF is inside `if DEBUG_INFO` and selects nothing. It gains an
AND-ed prerequisite clause naming the DWARF choice members, so the advertised set says "pick a
DWARF member, then BTF" rather than offering a symbol that will be dropped.

### 2. A clause carries a built-in requirement; the initrd carve-out is evaluated at the seam

`KernelConfig` starts recording the value it parsed: `enabled` keeps its present meaning (`=y` or
`=m`) so no existing reader changes, and a `builtin` set holds the `=y` subset, with
`builtin <= enabled` asserted at construction. `parse.py` populates both from the one regex it
already has.

A clause carries a three-valued built-in requirement, because the two reasons a symbol must be
`=y` are not the same reason and do not relax under the same condition:

| value | meaning | example |
|---|---|---|
| `NOT_REQUIRED` | `=m` satisfies the clause | `KASAN`, `FTRACE`, `KCOV` — the default |
| `REQUIRED` | `=y` always; a module form does not deliver the feature | `SERIAL_8250` (`SERIAL_8250_CONSOLE` `depends on SERIAL_8250=y`), `IKCONFIG` (`/proc/config.gz` exists only while the module is loaded) |
| `UNLESS_INITRD` | `=y` unless the build uploaded an initrd artifact | `EXT4_FS`/`XFS_FS`, `VIRTIO_BLK`, `VIRTIO_PCI` — nothing loads a module before root is mounted |

The carve-out is a fact about the build, not about the clause, so the clause states the condition
and the seam supplies the answer. The support checks take a keyword `has_initrd: bool = False`;
`rootfs_mount_warning` passes `installed_initrd_ref(conn, run_id) is not None`
(`services/runs/steps.py`). The default is the strict reading, so a caller that forgets over-warns
rather than falling silent — the direction ADR-0330 already chose for this warning.

### 3. A clause carries an arch scope; `FeatureRequirement` gains no arch axis

A clause carries `arches: frozenset[str] | None`, `None` meaning every arch. `serial_console`
becomes `{SERIAL_8250}` (`REQUIRED`, x86_64), `{SERIAL_8250_CONSOLE}` (x86_64), `{HVC_CONSOLE}`
(ppc64le), and `{VIRTIO_PCI}` (`UNLESS_INITRD`, every arch). The support checks take a keyword
`arch: str | None = None` and skip a clause scoped to a different arch, or scoped at all when the
arch is unknown — never inventing a requirement kdive cannot establish.

The axis belongs on the clause and not on `FeatureRequirement` because only one clause of one
feature varies by arch, and because `feature_manifest()` feeds a single static, unparameterized
resource document (`mcp/resources/external_build_contract.py`) with no request context to resolve a
per-feature arch against. A clause-level tag renders into that document as metadata the agent — who
knows its own target arch — filters on, while a feature-level axis would split every entry an agent
reads to vary one line.

### 4. `debuginfo` advertises the DWARF choice members, and only those

The group becomes `{DEBUG_INFO_DWARF5, DEBUG_INFO_DWARF4, DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT}` —
exactly the three choice members that `select DEBUG_INFO` and yield DWARF. `DEBUG_INFO_BTF` leaves
the group. It keeps its existing home under `bpf_tracing`, where BTF is the point; adding it to
`debuginfo` as an AND-ed clause instead would tell every offline-vmcore and gdb user that a DWARF
build is not enough, which is false and contradicts that entry's own summary.

The `debuginfo_warning` seam keeps keying on its module-level `DEBUG_INFO_BTF` literal
(`kernel_config/gate.py`). That is not drift: the seam asks whether in-guest drgn can read
`/sys/kernel/btf`, which is a different question from whether the kernel carries DWARF, and it is
deliberately not tied to a feature entry's advertised set. Its remediation string is corrected
under rule 1 in the same change — "enable `CONFIG_DEBUG_INFO_BTF`" alone is the same unfollowable
advice on a `DEBUG_INFO=n` kernel, so it must name a DWARF choice member too.

### 5. The refusal/warning payload keeps its shape and gains one optional key

`{reason, missing, remediation}` stands. `missing` remains the flat sorted union of the symbols of
every unmet clause, which under rule 1 is now always actionable. When a clause is unmet because its
member is present but modular, the payload adds `built_in_required: [symbols]`; the key is absent
when empty, so a client keying on the ADR-0330 shape is unaffected. This amends ADR-0330 rather
than superseding it: without the key, `missing` naming `VIRTIO_BLK` against a config containing
`CONFIG_VIRTIO_BLK=m` reads as a kdive bug to the agent holding that config.

### 6. Manifest schema

A `requirements` element stops being a list of symbol names and becomes an object:
`{"symbols": [...], "built_in": "...", "arch": [...]}`, with `built_in` and `arch` omitted at their
defaults so an unconstrained clause stays as small as it is today. The element-shape change bumps
the contract document's `schema_version` to `2`, once, in the first change to land. Adding an
optional key later does not bump it again.

### 7. Two invariants, pinned by tests

- **I1** — no clause member is a prompt-less or auto-selected symbol. Enforced by a deny-list test
  over every clause of every feature, seeded with the symbols the registry's own comments already
  name as unsettable: `KEXEC_CORE`, `VMCORE_INFO`, `DEBUG_INFO`, `BPF_EVENTS`, `TRACING`,
  `DYNAMIC_FTRACE`. The list grows as symbols are verified against Kconfig; it is not a claim that
  every clause has been audited.
- **I2** — no `gate_required` clause is arch-scoped or `UNLESS_INITRD`. Both depend on a fact the
  refusal seams do not supply, and a skipped or defaulted clause in a refusal set fails in the
  wrong direction. A future gated clause needing either must first make its seam pass the fact.

A third test asserts every `arches` value is a subset of `SUPPORTED_ARCHES`, from the test tree
only — `kernel_config` takes no runtime import on `domain.platform`.

### 8. Landing order

The four issues share one file and land serially. #1854 introduces the `Clause` record (symbols
only), the manifest object element and the `schema_version` bump alongside its own content fix and
I1, so the later three write against the settled shape. #1855 is then content only. #1860 adds the
built-in value, `KernelConfig.builtin`, the seam carve-out and the payload key. #1859 adds `arches`
and I2 last.

## Consequences

- **`Clause` stops being a `frozenset`.** Every read site inside `kernel_config` and its tests
  moves to `clause.symbols`; `_plain` keeps its signature. The type is package-internal, so no
  other package changes.
- **The contract resource changes shape for agents.** `schema_version` 2 is the signal; the
  document is generated, has no committed golden, and its one consumer is the resource itself.
- **Two clauses leave the refusal set and nothing replaces them.** A config that is internally
  inconsistent about `KEXEC_CORE` or `VMCORE_INFO` now arms instead of refusing. That is the
  fail-open trade ADR-0318 already makes for a config kdive cannot correlate with a kernel.
- **Two tests that pin the present behaviour must change, not be worked around.**
  `tests/kernel_config/test_requirements.py` asserts the derived pair is still in `gate_required`,
  and `tests/mcp/lifecycle/test_vmcore_tools.py` builds its refusal scenario by removing exactly
  `KEXEC_CORE` from an otherwise complete config — under this decision that config is satisfied and
  the scenario has to drop a settable symbol instead.
- **`rootfs_mount_warning` gains a second DB read** for the Run's `initrd_ref`, on the path that
  already reads the config object. A Run with no initrd is the common case and the strict default
  is what it gets.
- **A modular boot symbol starts warning where it was silent.** An agent who uploaded
  `CONFIG_VIRTIO_BLK=m` with no initrd and completed a build cleanly will now get
  `kernel_missing_boot_config`. That is the point of #1860; it is a warning, and the completion
  still succeeds.
- **`serial_console` stays advertise-only.** No seam reads it, so the arch scope and the built-in
  values on it are manifest metadata today. That is also why arch-scoped clauses can be skipped on
  an unknown arch without an operative consequence.
- **The clause is now three-part and can grow a fourth.** The rule that keeps it from doing so is
  rule 1: anything expressible as a prerequisite clause stays a clause rather than becoming a
  field.

## Considered & rejected

- **Keep `KEXEC_CORE`/`VMCORE_INFO` gated and annotate them as derived, naming selectors in the
  remediation.** The issue's other option. Rejected because the annotation buys nothing: the
  selector clauses are already in the same refusal set, so the annotated clause can never add a
  symbol to `missing` that is not already there, and the only config where it changes the verdict
  is the inconsistent one where refusing is wrong.
- **Give the clause a `remedy` set — the settable symbols to name when detection and remediation
  differ.** This was the general form of rule 1 and it covers the same three cases. Rejected as a
  field that an AND-ed prerequisite clause already covers, with worse detection: a `remedy` can
  only advise `SERIAL_8250`, whereas a `{SERIAL_8250}` clause can also report that it is missing.
- **An OR-group `{SERIAL_8250_CONSOLE, HVC_CONSOLE}` with no arch axis.** The shape #1859 names as
  fitting the existing model. Rejected on both directions of error: an x86 kernel carrying only
  `HVC_CONSOLE` reads as satisfied, and the `SERIAL_8250` prerequisite cannot be added at all
  without handing ppc64le a requirement that does not exist there.
- **An arch axis on `FeatureRequirement`.** Rejected: one clause of one feature varies, and the
  manifest is a static document with no request context, so a feature-level axis doubles what every
  agent reads in order to vary one line.
- **A single `builtin: bool`.** Rejected: it forces `SERIAL_8250`'s Kconfig-level `=y` and the boot
  ordering `=y` into one value, and an uploaded initrd relaxes only the second. Collapsing them
  either faults a modular-8250 kernel that uploaded an initrd, or stops asking for `SERIAL_8250=y`
  at all.
- **Per-symbol rather than per-clause built-in and arch values.** No clause in the registry mixes
  symbols that differ on either axis — `{EXT4_FS, XFS_FS}` are both tristate, the console symbols
  are separate clauses. Rejected until one does.
- **Change `_ENABLED` to match `=y` only.** #1860 names this and rejects it; recorded here because
  it is the change a reader reaches for first. `=m` is the right answer for KASAN, ftrace, kcov and
  BPF symbols, so a blanket change trades one wrong answer for a dozen.
- **Promote `DEBUG_INFO_BTF` to an AND-ed clause of `debuginfo`.** Reachable and consistent with
  the `debuginfo_warning` seam, but it redefines the feature: a DWARF-only build is what an offline
  vmcore and gdb need, and BTF costs a pahole pass and build time the entry's summary already
  prices as optional.
- **Leave `DEBUG_INFO_BTF` in `debuginfo` and mention its DWARF prerequisite in the summary
  prose.** Rejected for the reason #1859 gives about `SERIAL_8250`: the machine-readable array is
  what an agent diffs its config against, and prose it does not read is not a fix.
- **Replace `missing` with a list of clause objects.** It would carry the built-in requirement
  without a second key and is the shape the payload would have if designed now. Rejected as a
  breaking change to an ADR-0330 contract that three seams and their clients share, for a gain one
  optional key delivers.
- **Do nothing and let the four issues each pick a model.** Rejected as the state this record
  exists to prevent: four incompatible extensions to one type and a four-way conflict in one file.
