# 0544 — One clause model: settable symbols, a built-in requirement, an arch scope

## Status

Accepted (2026-08-05)

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

The rule has two halves, because "the agent cannot set this" has two causes:

- A symbol that is unsettable **in principle** — prompt-less, or existing only because something
  else `select`s it — is never a clause member, in `advertised` or in `gate_required`. The clause
  naming its settable selector stands in its place.
- A symbol that is settable **once a prerequisite holds** stays a clause member, and the
  prerequisite becomes its own clause, AND-ed alongside it in the same feature.

The second half is what keeps `SERIAL_8250_CONSOLE` and `DEBUG_INFO_BTF` — both real prompts that
`olddefconfig` drops on an unprepared config — as the symbols the registry names, rather than
hiding them behind whatever satisfies them.

This settles #1854 without adding a field. `crash_capture.gate_required` drops `{KEXEC_CORE}` and
`{VMCORE_INFO}`; the sibling clauses `{KEXEC, KEXEC_FILE}` and `{CRASH_DUMP}` already sit in the
same refusal set, and because `KEXEC`/`KEXEC_FILE` `select KEXEC_CORE` and `CRASH_DUMP` `select`s
`VMCORE_INFO`, no config `olddefconfig` can produce lacks a derived symbol while its selector is
present. The two removed clauses can therefore only fire on an internally inconsistent upload — a
truncated or hand-edited file — where refusing is exactly the false refusal ADR-0318's fail-open
boundary exists to avoid. The refusal for a kernel that genuinely cannot kexec is unchanged, and it
now names only symbols a fragment can set, which makes ADR-0330's existing remediation sentence
true rather than requiring a new one.

The second half applies to `bpf_tracing`, whose `{DEBUG_INFO_BTF}` clause is unreachable from a
bare config for the reason #1855 gives: BTF is inside `if DEBUG_INFO` and selects nothing. It gains
an AND-ed prerequisite clause naming the DWARF choice members, so the advertised set says "pick a
DWARF member, then BTF" rather than offering a symbol that will be dropped.

### Amendment (2026-08-05): the dropped clauses also fired on a whole class of working kernel (#1869)

This is an amendment rather than a new decision because it qualifies the reasoning above without
changing what was decided: the two clauses are still dropped, for the same rule, and no field is
added. It qualifies the claim that the removed clauses "can only fire on an internally
inconsistent upload — a truncated or hand-edited file".

That claim is narrower than the truth, and understated the defect being fixed. `VMCORE_INFO` did
not exist before Linux 6.9; it was split out of `CRASH_CORE` by `443cbaf9e2fd`. A complete,
coherent, working pre-6.9 config therefore could not satisfy the `{VMCORE_INFO}` clause at any
setting, and no rebuild could make it — the gate refused crash capture over a symbol absent from
that kernel's Kconfig entirely. This was not hypothetical: the `rocky-kdive-ready-8` (4.18) and
`rocky-kdive-ready-9` (5.14) images in `fixtures/local-libvirt/rootfs_catalog.toml` were
permanently unable to arm crash capture, reported against an unactionable symbol name.

So the removal fixes a live defect on a supported image class, not only a corner case on a
malformed upload. It strengthens the decision: the fail-open trade recorded above is the *lesser*
of the two reasons to drop these clauses. Filed as #1869 and closed by the change that implements
rule 1; the pre-6.9 kernel is that change's headline regression test, and reverting `{VMCORE_INFO}`
into `gate_required` reddens it. Verified against upstream `v4.18` and `v5.14` Kconfig, symbol by
symbol; the check is against upstream rather than Rocky's patched kernels, and those images carry
no in-tree `.config`, so the verified claim is the symbol-level one.

### 2. A clause carries a built-in requirement; the initrd carve-out is evaluated at the seam

`KernelConfig` starts recording the value it parsed: `enabled` keeps its present meaning (`=y` or
`=m`) so no existing reader changes, and a `builtin` set holds the `=y` subset, with
`builtin <= enabled` asserted at construction. `parse.py` populates both from the one regex it
already has. **`builtin` does not default.** A default of empty silently reinterprets every
existing positional fixture (`KernelConfig(frozenset({...}))`) as a wholly modular kernel, which
fails an `UNLESS_INITRD` clause for a reason the test never intended, and a helper does not stop
the next fixture from being written positionally. A non-defaulting field makes `ty` name every
site, which is the only thing that actually forces the migration.

The cost is real and belongs here rather than in the surprise: roughly **45 construction sites**,
concentrated in `tests/kernel_config/` and in the MCP debug and lifecycle tool tests, plus the one
real construction in `parse.py`. Most take the same value twice (a fixture that means "all built
in"), so the migration is mechanical — but it is dozens of sites, not the two the payload tests
make visible. (A count, not a census: this record is immutable once merged and the four changes it
governs all add fixtures, so read the magnitude and re-count at the time.)

A clause carries a three-valued built-in requirement, because the two reasons a symbol must be
`=y` are not the same reason and do not relax under the same condition:

| value | meaning | example |
|---|---|---|
| `NOT_REQUIRED` | `=m` satisfies the clause | `KASAN`, `FTRACE`, `KCOV` — the default |
| `REQUIRED` | `=y` always; a module form does not deliver the feature | `SERIAL_8250` (`SERIAL_8250_CONSOLE` `depends on SERIAL_8250=y`), `IKCONFIG` (`/proc/config.gz` exists only while the module is loaded) |
| `UNLESS_INITRD` | `=y` unless the build uploaded an initrd artifact | `EXT4_FS`/`XFS_FS`, `VIRTIO_BLK`, `VIRTIO_PCI` — nothing loads a module before root is mounted |

The carve-out is a fact about the build, not about the clause, so the clause states the condition
and the seam supplies the answer. The support checks take a keyword `has_initrd: bool = False`, and
so does `rootfs_mount_warning`: its caller `_success_envelope`
(`mcp/tools/lifecycle/runs/complete_build.py`) already holds the finalized `BuildStepResult` and
passes `result.initrd_ref is not None`. It does not re-read the row through
`installed_initrd_ref` — the value is in hand, and a second read would make the warning depend on
the build-step row being visible on that connection at that moment. The default is the strict
reading, so a caller that forgets over-warns rather than falling silent — the direction ADR-0330
already chose for this warning.

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
every unmet clause, which under rule 1 is now always actionable — a symbol that is enabled but
modular is in it, because its clause is unmet. `built_in_required` is the subset of `missing` that
the config enables as `=m`, so the agent can tell "you do not have this" from "you have this in a
form that cannot load in time". The key is absent when that subset is empty, so a client keying on
the ADR-0330 shape is unaffected. This amends ADR-0330 rather
than superseding it: without the key, `missing` naming `VIRTIO_BLK` against a config containing
`CONFIG_VIRTIO_BLK=m` reads as a kdive bug to the agent holding that config.

### 6. Manifest schema

A `requirements` element stops being a list of symbol names and becomes an object:
`{"symbols": [...], "built_in": "...", "arch": [...]}`, with `built_in` and `arch` omitted at their
defaults so an unconstrained clause stays as small as it is today. The element-shape change bumps
the contract document's `schema_version` to `2`, once, in the first change to land. Adding an
optional key later does not bump it again.

Two notes for whoever lands it. The shape change needs a test that pins the **element shape** —
the existing assertions are substring matches over `json.dumps(entry["requirements"])`, which pass
identically against a list of strings and against an object, so the change would otherwise land
with nothing holding it. And between #1854 and #1860 the v2 document carries no `built_in` on any
clause, which reads as "every boot clause accepts `=m`" — the defect #1860 fixes. No shape lie
exists in that window (the bump and the object both land in #1854); the content is simply not
there yet, and the interval is however long the two changes are apart.

### 7. Two invariants, pinned by tests — and what they do not catch

Both are regression guards, not proofs. Say so at the test, because a guard mistaken for a proof
is worse than no guard.

- **I1** — no clause member is unsettable in principle, the first half of rule 1. It says nothing
  about a symbol that is settable behind a prerequisite: `SERIAL_8250_CONSOLE` and
  `DEBUG_INFO_BTF` are clause members by design and are not deny-list candidates. Enforced by a
  **deny-list** over every clause of every feature, seeded with the symbols the registry's own
  comments already name as unsettable: `KEXEC_CORE`, `VMCORE_INFO`, `DEBUG_INFO`, `BPF_EVENTS`,
  `TRACING`, `DYNAMIC_FTRACE`. **It catches the return of a symbol already known to be
  unsettable. It cannot catch a seventh**: nothing in a `.config` distinguishes a prompt-less
  symbol from a prompted one, so the only real check is a human reading Kconfig, and the list
  grows as symbols are verified. It is not a claim that every clause has been audited.
- **I2** — a clause is arch-scoped only where **every seam that evaluates its feature supplies an
  arch**. That is the rule; `gate_required` is not the boundary. `rootfs_mount` has
  `gate_required=()` and is read live by `rootfs_mount_warning` through
  `unmet_advertised_clauses`, so it is seam-evaluated on its *advertised* clauses, and rule 3
  would silently skip an arch-scoped clause there because `complete_build` has no arch —
  a warning that vanishes rather than fires. Today the seam-evaluated features are `crash_capture`
  (gated) and `rootfs_mount` (advertised), neither of which supplies an arch, so neither may carry
  an arch-scoped clause; `serial_console` is read by no seam and may. The same rule covers
  `UNLESS_INITRD`, whose one supplying seam is `rootfs_mount_warning`. **I2 checks declared field
  values against a hand-maintained list of seam-evaluated features. A clause that is arch-specific
  in fact while carrying `arches=None` passes it vacuously** — see the residual below, which is
  exactly that case.

A third test asserts every `arches` value is a subset of `SUPPORTED_ARCHES`, from the test tree
only — `kernel_config` takes no runtime import on `domain.platform`.

**The residual I2 does not close.** The gated pair is `{FW_CFG_SYSFS}` and `{RELOCATABLE}` in
`crash_capture.gate_required`; `RANDOMIZE_BASE` is in the same entry's *advertised* set only —
ADR-0318's worked example of a symbol deliberately advertised and not gated, because KASLR-off
debug kernels are routine — so its arch-conditionality is an advisory-quality question and can
never produce a refusal. At least `FW_CFG_SYSFS` is plausibly unavailable on the `pseries` machine
type kdive boots for `ppc64le` — this is **unverified against Kconfig** and is recorded as a
question, not a finding. If it holds, a ppc64le kernel is refused crash capture over a symbol it
cannot set, which is #1854's defect on a second feature. This ADR does **not** resolve it and #1859 does **not** tag `crash_capture`: doing so
would need the refusal seams (`install` crashkernel reservation, kdump vmcore fetch) to resolve
the Run's System's profile arch, which is real work outside all four issues. The residual is
deferred to **#1875** rather than left implicit, and I2 keeps `crash_capture` untagged in the
meantime — the honest state is "unhandled and recorded", not "guarded".

### 8. Landing order

The four issues share one file and land serially. #1854 introduces the `Clause` record (symbols
only), the manifest object element and the `schema_version` bump alongside its own content fix and
I1, so the later three write against the settled shape. #1855 is then content only. #1860 adds the
built-in value, `KernelConfig.builtin`, the seam carve-out and the payload key. #1859 adds `arches`
last.

**I2 lands in two halves, each with the field it guards.** It covers two axes, and they arrive one
PR apart: #1860 lands the `UNLESS_INITRD` half alongside the value, and #1859 lands the arch half
alongside `arches`. Deferring the whole invariant to #1859 would leave #1860 — the change that
marks `rootfs_mount` and `serial_console` `UNLESS_INITRD` — free to mark a seam-evaluated feature
whose seam supplies nothing, with nothing to stop it.

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
- **`rootfs_mount_warning` gains a parameter, not a query.** The initrd fact reaches it from the
  `BuildStepResult` its caller already holds, so the carve-out costs no extra read and no new
  visibility assumption. A seam that ever calls it without that value gets the strict default.
- **A modular boot symbol starts warning where it was silent — including on kernels that boot
  fine.** An agent who uploaded `CONFIG_VIRTIO_BLK=m` with no initrd and completed a build cleanly
  will now get `kernel_missing_boot_config`. That is the point of #1860. But
  `result.initrd_ref is not None` means "this build uploaded an initrd artifact", **not** "this
  kernel can load a module before root is mounted": a kernel with an embedded initramfs
  (`CONFIG_INITRAMFS_SOURCE`) has `initrd_ref is None` — the class
  `providers/local_libvirt/lifecycle/install.py` documents — and will draw a spurious warning.
  Accepted rather than modelled: kdive cannot see inside the kernel image, the signal is a warning
  the completion survives, and over-warning is the direction ADR-0330 chose. It is a false
  positive on a working kernel, not only a true one on a broken kernel.
- **#1854 leaves a served doc contradicting the payload it fixes.** After it lands, `missing`
  names only settable symbols while `docs/operating/external-build-upload.md` — served as
  `resource://kdive/docs/operating/external-build-upload.md`, the doc the remediation strings link
  to — still tells the agent to set `CONFIG_VMCORE_INFO`. **#1853 owns that file set and is fixing
  it in the same campaign**, so this record deliberately does not touch it and #1854 must not
  either. #1854 is not complete as a user-visible fix until #1853 lands beside it.
- **`serial_console` stays advertise-only.** No seam reads it, so the arch scope and the built-in
  values on it are manifest metadata today. That is also why arch-scoped clauses can be skipped on
  an unknown arch without an operative consequence.
- **Every Kconfig line reference in this record is unverified and will drift.** The citations
  (`kernel/Kconfig.kexec:11` and `:8`, `lib/Kconfig.debug`, `drivers/tty/serial/8250/Kconfig:70-72`)
  were taken from the issues and the registry's own comments, which name Linux v7.0 but are not
  pinned here to a tag this repository can check. Line numbers move between releases; treat them as
  pointers to a symbol, and re-read the symbol before acting on a number.
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
- **No arch representation at all** — correct `serial_console`'s clause content and leave the arch
  split to the summary prose, which #1851 already wrote. The cheapest option, and tempting because
  the field's only user is advertise-only: no seam reads `serial_console`, so `arches` changes no
  behaviour today. Rejected because it reproduces the defect #1859 files — the machine-readable
  array is what an agent diffs its config against, and prose it does not parse is not a fix — and
  because a clause tag is what stops a later seam faulting a ppc64le kernel for `SERIAL_8250`.
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
