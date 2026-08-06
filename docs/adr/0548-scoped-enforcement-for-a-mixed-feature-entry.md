# 0548 — A mixed feature entry publishes the scoped enforcement of the symbols kdive really reads

## Status

Accepted (2026-08-05)

## Context

[ADR-0546](0546-manifest-states-where-an-omission-surfaces.md) replaced the manifest's `gated`
bool with `enforcement`, a closed vocabulary naming *where an omission surfaces*. It closed the
misreading #1867 filed, and it recorded two things it did not close:

- **`bpf_tracing` reads `unchecked` although one of its symbols draws a warning** (Consequences).
  `debuginfo_warning` (`src/kdive/kernel_config/gate.py`) keys on a module-level `DEBUG_INFO_BTF`
  literal rather than on a feature entry, so no seam calls `feature_requirement("bpf_tracing")` and
  `unchecked` is literally true — while the agent that omits `DEBUG_INFO_BTF` gets a
  `missing_debuginfo` warning at `debug.start_session` (drgn-live) and at the live `introspect.run`
  and `introspect.script` seams. Filed as **#1901**.
- **`enforcement` is a per-entry summary and cannot express a mixed entry** (Consequences). "No
  entry today has clauses enforced at different points. The first one that does needs a decision,
  not a data edit."

`bpf_tracing` **is** that first mixed entry, and it was already one when ADR-0546 landed. Its
`advertised` tuple is five clauses — `{BPF_SYSCALL}`, `{PERF_EVENTS}`,
`{KPROBE_EVENTS, UPROBE_EVENTS}`, the DWARF choice members, and `{DEBUG_INFO_BTF}`. Exactly one of
the five is read by a kdive seam. The other four are read by nothing, at any point, and
`unchecked` is the honest value for them.

So the defect is not that `enforcement` holds the wrong value. It is that one value per entry
cannot hold the right answer for an entry whose clauses differ, and #1901's two candidate fixes
both assume it can.

[ADR-0544 §4](0544-kernel-config-clause-model.md) is the other constraint, and it is unamended.
It rejected tying the `debuginfo_warning` seam to a feature entry on its merits: the seam asks
whether in-guest drgn can read `/sys/kernel/btf`, which is a different question from whether the
kernel carries a feature's advertised set. Its closing consequence also holds a `Clause` to three
axes, all of them facts about the kernel.

## Decision

### 1. An entry may carry `also_checked`, a scoped enforcement statement over a subset of its clauses

The manifest entry gains an optional key `also_checked`: a list of objects, each naming one
advertised clause and stating the enforcement that applies to *that clause* rather than to the
entry. It is present only when a seam reads a strict subset of an entry's symbols, so exactly one
entry carries it today:

```json
{
  "feature": "bpf_tracing",
  "enforcement": "unchecked",
  "requirements": [ ... five clauses ... ],
  "also_checked": [
    {
      "symbols": ["DEBUG_INFO_BTF"],
      "enforcement": "runtime_advisory",
      "reason": "missing_debuginfo",
      "surfaces_at": ["debug.start_session", "introspect.run", "introspect.script"]
    }
  ]
}
```

`enforcement` on the entry keeps its meaning unchanged — it describes the entry as a whole, and
`unchecked` remains true of the four clauses no seam reads. `also_checked` is the exception list,
and the agent that omits `DEBUG_INFO_BTF` now reads its consequence out of the machine-readable
payload rather than inferring it.

The element embeds a clause object in the same shape as `requirements` and `refuses_on`
(`{"symbols": [...], "built_in": "...", "arch": [...]}`, keys omitted at their defaults), so a
client that already parses one clause parses all three. The clause must be one the entry
*advertises* — asserted at construction — so a scoped statement can never point at a symbol the
agent cannot see in the same entry.

`reason` is the literal string the warning payload carries in its `reason` field, so the agent can
correlate the contract entry with the response it receives rather than pattern-matching prose.
`surfaces_at` names the tools that emit it.

This is the same shape rule ADR-0546 §2 applied to `refuses_on`: a separate key carrying its own
clause list, not a flag painted onto `requirements`. Two reasons, both of which held there too. A
per-clause `enforcement` field would put a fact about kdive on a record ADR-0544 reserves for
facts about the kernel, and it would make every clause carry a key that four out of five clauses
of one entry — and every clause of the other fifteen entries — would spend at its default.

### 2. The vocabulary gains `runtime_advisory`, and it is legal only inside `also_checked`

The four existing values cannot describe this seam. `upload_advisory` is close — the legend
sentence ("kdive reads the `effective_config` you uploaded with the build and returns a warning in
the response `data`") describes the observed behavior almost verbatim — but the value's name
asserts the upload lane, and the warning arrives at a debug seam on a kernel already built,
installed and booted. That difference is exactly the one ADR-0546 rule 1 exists to publish.
`runtime_refusal` has the right seam and the wrong outcome; the action succeeds.

So the vocabulary gains a fifth value, `runtime_advisory`: kdive reads the uploaded config, but
only when the agent invokes a seam that needs these symbols, and it warns rather than refusing.
It fills the fourth cell of the grid the other three occupy (upload/runtime × refusal/advisory).

Entry-level use is rejected at construction. `runtime_advisory` on the entry is #1901's option 2,
and it is false in the same way `unchecked` is under-stated: it would label five clauses for the
behavior of one, telling an agent that omitting `BPF_SYSCALL` draws a `missing_debuginfo` warning
when a kernel with BTF and no `BPF_SYSCALL` draws nothing — BTF is readable. A value that can only
appear scoped is the enforcement of the reasoning above, not a second convention.

### 3. The scoped value is grounded in `gate.py`'s constants, not authored

ADR-0546 rule 1 grounds `upload_refusal` and `upload_advisory` by scraping `gate.py`'s
`feature_requirement(...)` call sites, and concedes that `runtime_refusal` "has no such derivation
and cannot have one".

`runtime_advisory` does have one, and a stronger one than the call-site scrape. The seam it
describes keys on two module-level constants in `gate.py` — the symbol (`_BTF_SYMBOL`) and the
payload's reason string (`MISSING_DEBUGINFO_REASON`) — and both are exactly the values the
`also_checked` element publishes. A test asserts that identity, so the registry cannot claim a
symbol or a reason the seam does not emit.

`surfaces_at` cannot be derived the same way: no mechanical map goes from a Python call site to an
MCP tool name. What *is* mechanical is the set of modules that call `debuginfo_warning`, so a test
pins that set. A third module wiring the seam in fails it and forces `surfaces_at` to be
re-verified in the same change — the same "force a re-verify when the scope grows" guard the
arch-scope work used, rather than a value that silently goes stale.

ADR-0546 rule 1's own grounding guard is **unchanged**. No entry claims an `upload_*` value it did
not claim before, `bpf_tracing` stays `unchecked`, and nothing new is wired into `gate.py`. That
the fix leaves that guard intact is evidence for the shape, not a coincidence: the guard asserts
which *entries* a seam reads, and this seam still reads no entry.

### 4. `schema_version` stays at 3

[ADR-0544 §6](0544-kernel-config-clause-model.md) recorded that adding an optional key later does
not bump the version, and ADR-0546 §4 bumped to `3` for the other case — a key removed and a key
renamed. This change is the first case only.

`also_checked` is absent from fifteen of sixteen entries and from every entry that existed before
it, so an existing reader sees a byte-identical document for those. The fifth vocabulary value can
appear *only* inside `also_checked`, so a client that exhaustively matches `enforcement` never
meets it; `enforcement_legend` gains a definition, which is additive and is that object's purpose.
No existing reader breaks, so there is no signal to send.

### 5. The legend defines the new value and the new key, in the payload

ADR-0546 §3 is that a served flag ships with its served definition. `runtime_advisory` gets its
`ENFORCEMENT_LEGEND` sentence from the same enum-keyed mapping as the other four, so it cannot
exist undefined.

`also_checked` is a key rather than a value, so the enum-keyed legend cannot carry it.
`feature_config_requirements` gains `also_checked_legend`, one sentence, served beside
`enforcement_legend`. An agent that reads the resource has the meaning of every flag in it without
a second fetch — the property #1867 was filed for the absence of.

## Consequences

- **An agent that omits `DEBUG_INFO_BTF` learns the cost from the contract.** It reads
  `also_checked`, sees `runtime_advisory`, the `missing_debuginfo` reason string and the three
  seams, and can decide before building rather than discovering it after an install and a boot.
  #1901 is closed at the layer it occurs on.
- **`unchecked` keeps its literal meaning and gains a qualifier.** Its legend sentence still says
  no kdive check reads the entry's requirements — which stays true, because the seam reads a
  symbol and not the entry — and now points at `also_checked` for the exception. The
  under-statement ADR-0546 recorded is what the pointer removes.
- **The `debuginfo` entry is untouched.** Its clauses are the DWARF choice members, which no seam
  reads at any point (ADR-0544 §4), so `unchecked` is accurate for it under any reading and it
  carries no `also_checked`.
- **`gate.py` is unchanged.** ADR-0544 §4's rejection stands unamended, and this record does not
  reopen it: the seam still keys on its own literal and still asks its own question. What changed
  is only that the registry publishes what that seam does.
- **A second mechanism exists for stating enforcement, and that is the cost.** An agent must read
  `enforcement` *and* `also_checked` to know what kdive checks, where one key sufficed. The
  alternative is a per-entry value that is false for four clauses out of five, which is the defect
  rather than a cheaper fix. `also_checked` is absent when it would be empty, so the entries with
  nothing to qualify are unchanged.
- **ADR-0546 rule 1's closed vocabulary is now five values, and its Consequences record two claims
  this record qualifies.** An amendment on that record points here rather than restating the
  decision, per the partial-supersession rule in `docs/adr/README.md`.
- **The served upload doc names the fifth value and the new key.** Its one paragraph on
  `enforcement` enumerates the vocabulary in prose ("refused at upload, warned at upload, refused
  later on a booted kernel, or never checked"), which a fifth value makes incomplete. It gains the
  fifth reading and one clause naming `also_checked`, and still does not restate the legend —
  the drift surface ADR-0546 kept it away from.

## Considered & rejected

- **Have `debuginfo_warning` read `feature_requirement(BPF_TRACING)` instead of its literal** —
  #1901's option 1, and the one that would need no new contract key at all. Rejected twice over.
  ADR-0544 §4 rejected it on its merits and is unamended: the seam asks whether in-guest drgn can
  read `/sys/kernel/btf`, not whether the kernel carries a feature's advertised set. And it would
  change the warning's *meaning* — the entry advertises five clauses, so a kernel with
  `DEBUG_INFO_BTF` and no `BPF_SYSCALL` would draw `missing_debuginfo`, which is false, because
  BTF is readable and drgn resolves symbols fine. Salvaging it needs a BTF-only sub-selection of
  the entry, which is the module-level literal under another name.
- **Relabel `bpf_tracing` to a fifth entry-level value such as `runtime_advisory`** — #1901's
  option 2, one field, no new key, no new legend entry. Rejected because it prices four clauses
  wrong to price one right. `BPF_SYSCALL`, `PERF_EVENTS`, the probe-event source and the DWARF
  members are read by nothing at any seam, and an entry-level `runtime_advisory` tells the agent
  that omitting any of them draws a warning. It also re-creates ADR-0546's own diagnosis one row
  down: a value that over-states is the same class of defect as one that under-states, and the
  entry would still not say *which* symbol is the checked one. Rule 2's construction-time
  rejection is this argument made unrepeatable.
- **Relabel `bpf_tracing` to `upload_advisory`** — the value whose legend sentence already
  describes the observed behavior, needing neither a new key nor a new vocabulary value.
  Rejected on two counts. The name asserts a seam that is wrong by an install and a boot: the
  agent learns at `debug.start_session`, not at `runs.complete_build`, and the distance between
  those is the entire point of ADR-0546's vocabulary. It would also require loosening rule 1's
  grounding guard, which asserts in both directions that the entries claiming an `upload_*` value
  are exactly the two `gate.py` reads by id — the guard would have to grow an exception for the
  one entry it is meant to catch.
- **State it in the `bpf_tracing` summary and change no keys** — zero contract change, and the
  summary already discusses BTF at length. Rejected for the reason ADR-0546 and ADR-0544 both gave
  about `SERIAL_8250` and `DEBUG_INFO_BTF`: the machine-readable array is what an agent diffs its
  config against, and prose it does not parse is not a fix. The summary does gain a sentence here,
  but as the qualifier on a key that carries the fact, not as the carrier.
- **Model the scoped value as a fourth `Clause` field** — per-clause granularity, no second array,
  and the value sits beside the symbols it qualifies. Rejected for ADR-0546's reason, which
  ADR-0544's closing consequence states first: a clause carries three axes and all three are facts
  about the kernel, while enforcement is a fact about kdive. It would also spend a key on every
  clause in the registry to carry one non-default value.
- **Add a `suppressed_by` key naming the uploaded-`vmlinux` escape hatch** — the warning really is
  suppressed when the Run carries a `debuginfo_ref`, so the contract over-claims slightly without
  it. Rejected as surface for one entry's one condition, when ADR-0546 rule 1 already puts what an
  omission *costs* in the summary, the emitted warning's own `remediation` names the vmlinux path,
  and the suppressor is a property of the Run rather than of the kernel config the manifest
  describes. The summary sentence carries it.
- **Bump `schema_version` to 4** — a fifth vocabulary value in a vocabulary documented as closed
  is arguably a contract change, and the bump is free. Rejected against ADR-0544 §6's recorded
  rule: an added optional key does not bump, and the new value is unreachable except through that
  key. A version bump that no reader can act on trains readers to ignore the next one.
