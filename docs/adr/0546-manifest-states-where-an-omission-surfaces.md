# 0546 — The feature manifest states where an omission surfaces, not whether kdive gates

## Status

Accepted (2026-08-05)

## Context

`feature_manifest()` (`src/kdive/kernel_config/requirements.py`) renders one entry per feature
keyed exactly `feature`, `summary`, `gated`, `requirements`, and its only consumer is the served
`resource://kdive/contracts/external-build` document
(`src/kdive/mcp/resources/external_build_contract.py`). [ADR-0318](0318-debug-feature-config-gate.md)
authorized that shape, with `gated` a bare bool derived from whether the entry carries a
`gate_required` clause tuple.

Nothing in the served document says what `gated` means. The agent reading it has to guess, and the
guess it reaches for is "true means I have to build this, false means I may skip it".

For most entries that guess is harmless and roughly right. For `sysrq` it is wrong, and #1873 made
it wrong *silently*. That change removed `sysrq`'s `gate_required` tuple — no seam read it, so the
entry was shipping `gated: true` against an upload path that checked nothing — and the entry
became `gated: false`. It is now structurally identical to `kasan`, `kcsan`, `kfence`, `kmemleak`,
`lockdep`, `ftrace` and `kcov`, which genuinely cost nothing to omit. `MAGIC_SYSRQ` is a plain bool
with no module form (`lib/Kconfig.debug`), so omitting it costs a rebuild, a reinstall and a
reboot, and the refusal arrives from `diagnostic_sysrq` at the far end of that round trip
(ADR-0318's "sysrq is runtime-gated, not pre-gated" boundary). What distinguishes it from the
seven cheap entries survives only in the summary prose — the layer #1861 itself called the wrong
one for the fix.

The same defect exists one level down, at finer granularity. `crash_capture` ships `gated: true`
and seven advertised clauses, and the entry says nothing about which of them can actually produce a
refusal. Six can; `RANDOMIZE_BASE` cannot — it is ADR-0318's worked example of a symbol
deliberately advertised and not gated, because KASLR-off debug kernels are routine. ADR-0544's
[2026-08-05 amendment](0544-kernel-config-clause-model.md) then tagged two of the advertised
clauses `x86_64`-only, which sharpens the question rather than answering it: a `ppc64le` agent now
reads an entry whose advertised set names two symbols it cannot set and one that can never refuse,
with no key distinguishing any of them.

Both are the same defect. The manifest ships flags and arrays without stating what they mean, and
the agent supplies a meaning that is sometimes false.

## Decision

### 1. `gated` is replaced by `enforcement`, which names where an omission surfaces

The entry key `gated` is removed. In its place each entry carries `enforcement`, a string from a
closed vocabulary that answers the question the agent is actually asking — *if I omit these
symbols, when and how do I find out?*

| value | meaning | entries today |
|---|---|---|
| `upload_refusal` | kdive reads the Run's uploaded `effective_config` and **refuses** the config-dependent action with `CONFIGURATION_ERROR` naming the missing symbols. The clauses it refuses on are the entry's `refuses_on` (rule 2). | `crash_capture` |
| `upload_advisory` | kdive reads the config and returns a **warning** in the response `data`; the action succeeds. | `rootfs_mount` |
| `runtime_refusal` | kdive does **not** read the config for this feature. The feature's own handler refuses when the agent invokes it, after the kernel has been built, installed and booted. | `sysrq` |
| `unchecked` | no kdive check reads this entry's requirements at any seam. | every other entry |

`unchecked` is deliberately worded as a statement about kdive and not about the feature's
importance, and its legend entry says so outright. `serial_console` is `unchecked` and its
`VIRTIO_PCI` clause is boot-fatal — a value that read as "safe to skip" would move #1867's
misreading one row down the table rather than closing it. What an omission costs varies per
feature and stays in the summary, which is the only field that can carry it.

The vocabulary is grounded in code, not in intent: `upload_refusal` and `upload_advisory` are
exactly the two features `kernel_config/gate.py` imports by id and turns into a payload
(`CRASH_CAPTURE` through `unmet_clauses`, `ROOTFS_MOUNT` through `unmet_advertised_clauses`), and
`runtime_refusal` is exactly ADR-0318's "enforced by the mechanism that can actually observe the
condition". A test derives the `unchecked` set from the roster rather than listing it, so a new
entry is covered without editing the test and a newly-enforced entry fails on purpose.

`enforcement` replaces `gated` rather than joining it. `gated` is exactly
`enforcement == "upload_refusal"`, so keeping both would be two spellings of one fact — and the
misreadable spelling is the shorter one, so a reader who skims keys on the field that caused the
defect. Removing it is what closes the misreading; adding a field beside it only helps the reader
who was already going to be careful.

### 2. An `upload_refusal` entry publishes the clauses it refuses on

The entry gains `refuses_on`: the `gate_required` clauses, rendered with the same clause-object
shape as `requirements` (`{"symbols": [...], "built_in": "...", "arch": [...]}`, keys omitted at
their defaults). It is present only when non-empty, so exactly one entry carries it today.

This is what closes the `crash_capture` granularity defect, and it has to be the clause list rather
than a per-clause flag on `requirements`, because the two sets are not in one-to-one
correspondence. `crash_capture` advertises `{KEXEC}` and `{KEXEC_FILE}` as separate clauses —
guidance to build both — while the gate takes them as the single OR-group `{KEXEC, KEXEC_FILE}`,
because either load syscall suffices. A boolean painted onto the advertised `{KEXEC}` clause would
read as "omit this and kdive refuses", which is false. Publishing the refusal set as its own array
of clauses keeps the OR semantics that make the statement true.

Nothing sensitive crosses the boundary: ADR-0318 already established that `CONFIG_*` names are
public knowledge and are the one thing the gate is permitted to name.

### 3. The legend ships in the document, beside the values it defines

`feature_config_requirements` gains `enforcement_legend`, an object mapping each vocabulary value
to a sentence defining it. The whole defect is a served flag with no served definition, so the
definition is served — an agent that reads the resource has the legend in the same payload and
needs no second fetch, no doc read, and no summary parse.

The legend is generated from the same enum the entries are rendered from, so a value can neither
appear in an entry without a definition nor be defined without existing.

### 4. `schema_version` goes to 3

An entry key is removed and a key is renamed. [ADR-0544 §6](0544-kernel-config-clause-model.md)
bumped the document to `2` for the clause element reshape and recorded that *adding* an optional
key later does not bump it again; removing a key that every entry carried is the other case. `3`
is the signal, bumped once here for all of rules 1-3 together.

The document is generated, has no committed golden, and its only in-repo consumer is the resource
that serves it (ADR-0544, Consequences), so the cost of the bump is the signal itself.

## Consequences

- **An agent can tell `sysrq` from `kasan` without reading prose.** `sysrq` is
  `runtime_refusal`; the seven cheap entries are `unchecked`; the legend states the difference and
  what the round trip costs. The misreading #1867 filed is closed at the layer it occurs on.
- **`crash_capture` states its refusal set.** A `ppc64le` agent can see that `RANDOMIZE_BASE` is
  advertised-only and that the two `x86_64`-scoped clauses do not apply, from the machine-readable
  arrays rather than from the summary.
- **The manifest now states one fact about kdive per entry, alongside facts about the kernel.**
  `requirements` and its clause keys describe the kernel; `enforcement` and `refuses_on` describe
  what kdive does. They are deliberately separate keys rather than a fourth clause axis: ADR-0544's
  closing consequence is that a clause grows no fourth field, and enforcement is not a property of
  a clause in any case.
- **A client keyed on `gated` breaks.** That is the point, and `schema_version` 3 announces it. No
  such client exists in this repository; an external one is an agent reading a served document
  whose version key it can check.
- **`bpf_tracing` reads `unchecked` although one of its symbols draws a warning.**
  `debuginfo_warning` (`kernel_config/gate.py`) keys on a module-level `DEBUG_INFO_BTF` literal at
  the drgn-live seams, deliberately not tied to any feature entry's advertised set (ADR-0544 §4) —
  so no seam reads the `bpf_tracing` entry, and `unchecked` is true as this record defines it
  ("no kdive check reads this entry's requirements") while under-selling what the agent will
  experience. Recorded rather than papered over: closing it means either tying that seam to an
  entry, which ADR-0544 rejected on its merits, or a fifth vocabulary value for one symbol. Filed
  as **#1901** rather than settled here.
- **`enforcement` is a per-entry summary and cannot express a mixed entry.** No entry today has
  clauses enforced at different points. The first one that does needs a decision, not a data edit —
  the same boundary ADR-0318 drew around the gated set.
- **The served upload doc points at the vocabulary and does not copy it.**
  `docs/operating/external-build-upload.md` already points at the resource for the per-feature
  manifest; it gains one sentence naming the `enforcement` key and the legend beside it, in the
  same form it already names the `built_in` clause key. It does not restate the values. A second
  prose copy of a generated legend is the drift surface ADR-0504 removed the ADR index for, and
  the surface that let that doc keep telling agents to set `CONFIG_VMCORE_INFO` after the manifest
  stopped naming it.

## Considered & rejected

- **Give the existing `gated` bool a legend and stop there** — the issue's first direction, and the
  cheapest change: add `gated_meaning` to the payload, define the flag, change no entry. Rejected
  because it defines the flag correctly and still leaves the defect standing. The honest definition
  is "true means kdive refuses the upload-lane action", so `gated: false` becomes *precisely*
  "kdive will not refuse" — which is true of `sysrq` and true of `kasan`, and an agent deciding
  what to skip is no better off than before. A legend that makes a misreading precise is not a fix;
  what the reader needs is a value that differs between the two entries. The legend survives as
  rule 3, attached to a vocabulary that does differ.
- **Keep `gated` and add `enforcement` beside it** — non-breaking, no `schema_version` bump, every
  existing client unaffected. Rejected as two mechanisms for one job: `gated` stays exactly
  derivable from `enforcement`, so the pair can only agree or be a bug, and the field that caused
  the misreading remains the cheapest one to key on. A deprecation window buys nothing here — the
  document is generated with no committed golden and one in-repo consumer, so there is no migration
  to stage.
- **Paint a `gated: true` flag on each `requirements` clause instead of publishing `refuses_on`** —
  fewer keys, no second array, and the flag sits next to the symbols it qualifies. Rejected as
  false in the one entry it would apply to: the advertised set splits `{KEXEC}` and `{KEXEC_FILE}`
  where the gate joins them, so per-clause flags would tell the agent that omitting `KEXEC` alone
  produces a refusal. The refusal set has its own OR-grouping and has to be published with it.
- **Model enforcement as a fourth `Clause` field** — it would give per-clause granularity and drop
  `refuses_on`. Rejected twice over: ADR-0544's closing consequence holds the clause to three axes,
  and those three are facts about the kernel (settability, module form, arch) while enforcement is
  a fact about kdive. Mixing the two on one record makes the clause a grab bag.
- **State the enforcement point in each summary and change no keys** — zero contract change, and
  the `sysrq` summary already says most of it (#1851). Rejected for the reason ADR-0544 gave twice
  about `SERIAL_8250` and `DEBUG_INFO_BTF`: the machine-readable array is what an agent diffs its
  config against, and prose it does not parse is not a fix. It is also the layer #1861 identified
  as the wrong one, and the summary that carries this today did not stop #1867 from being filed.
- **Add a numeric or ordinal "cost of omitting" score** — an agent could then sort entries by risk
  without interpreting a vocabulary. Rejected as a number kdive cannot ground: the cost of omitting
  `MAGIC_SYSRQ` is a rebuild-install-boot round trip, the cost of omitting `KASAN` is not finding a
  bug, and no scale ranks those honestly. Naming the mechanism is a fact; scoring it is an opinion.
- **Serve the enforcement vocabulary only from `docs/operating/external-build-upload.md`** — the
  doc is already a served resource and already explains the upload lane in prose. Rejected because
  it splits a generated document's meaning across two artifacts with no generator tying them, which
  is exactly how the doc came to tell agents to set `CONFIG_VMCORE_INFO` after the manifest stopped
  naming it (ADR-0544, Consequences).
