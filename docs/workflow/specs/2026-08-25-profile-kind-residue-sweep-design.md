# Detect Systems whose stored profile section does not match their Resource kind

Issue: [#1907](https://github.com/randomparity/kdive/issues/1907)
Decision record: [ADR-0579](../../adr/0579-stored-profile-kind-sweep-is-an-operator-cli-read.md)
Builds on: [ADR-0549](../../adr/0549-profile-policy-declares-its-resource-kind.md),
[ADR-0256](../../adr/0256-onboard-target.md)

## Goal

Give an operator one command that reports every stored System whose provisioning-profile provider
section is not the kind of the Resource its Allocation is bound to. It always exits `0`; the
printed report is the answer. Report only — no row is written, and no remediation is chosen.

## Background

ADR-0549 added the admission cross-check that rejects a kind-mismatched profile at
`systems.provision` and `systems.reprovision`. It runs on the write path only; a System minted
before it keeps its mismatched profile, and the check never runs on a stored-profile read so that
`control._op_opt_in`'s unguarded parse cannot start raising on stored data.

A **fault-inject** Resource holding a libvirt-section profile was accepted outright before
ADR-0549 and reaches `ready`, because the fault-inject provisioner discards the profile. It then
raises a bare `AttributeError` from `ProviderSection.fault_inject` at first use, on the four lanes
ADR-0549 names: `src/kdive/mcp/tools/lifecycle/control/registrar.py` (`destructive_opt_in`),
`src/kdive/services/runs/steps.py` (the Run install step's `install_method_for`),
`src/kdive/jobs/handlers/runs/boot_evidence.py` (`capture_method`), and
`src/kdive/mcp/tools/lifecycle/vmcore/handlers.py` (`capture_method`). Path and function only:
a line number rots and nothing keeps it accurate, so the report never carries one.

#1907's body lists `src/kdive/mcp/tools/debug/sessions/lifecycle.py` in place of the boot-evidence
step. That is wrong and this spec does not follow it: the only profile-policy call in that module
is `drgn_live_seeds_bootstrap_key` at line 445, and `FaultInjectProfilePolicy` returns `False`
from it without dereferencing a section. The correction changes nothing about the sweep — it
changes which lane the report tells an operator to look at.

The two libvirt kinds could not reach `ready`, so the expected residue is fault-inject Systems.

That bounds the candidate population hard: the fault-inject runtime "is opt-in and absent from
the default production composition (ADR-0071)"
(`src/kdive/providers/fault_inject/__init__.py:5-6`, gated by `_fault_inject_enabled` in
`providers/assembly/composition.py:189`). A deployment that never composed it holds no
`fault-inject` Resource and so no candidate row — the sweep is clean there by construction. The
sweep is worth building anyway (#1907 asks for detection, and "clean by construction" is a claim
worth being able to check rather than assert), but it sets the scale: this is a targeted check
for opted-in deployments, not a fleet-wide integrity problem.

## What "mismatched" means here

`ProvisioningProfile.provider` is required, and `_require_exactly_one_provider` admits exactly one
section. `dump_profile` (`src/kdive/profiles/provisioning.py:551`) serializes with
`model_dump(mode="json", by_alias=True, exclude_none=True)`, and each section's alias **is** the
`ResourceKind` value, so a stored profile's `provider` object holds exactly one key and that key
is a kebab-case kind string (`local-libvirt`, `fault-inject`, `remote-libvirt`). Both write paths
— `src/kdive/services/systems/admission.py:742` and
`src/kdive/mcp/tools/lifecycle/systems/admin.py:278` — go through `dump_profile`.

`resources.kind` is `text` with a `CHECK` over the same three values (`0001_init.sql`, widened by
`0018_resources_kind_fault_inject.sql` and `0020_resources_kind_remote_libvirt.sql`). The
comparison is therefore direct string equality between the stored JSON key and the column.

A System is **mismatched** unless its stored `provisioning_profile -> 'provider'` is **exactly**
`{<the bound Resource's kind>: {…}}` — one section, keyed by that kind, whose value is an object.
That covers the residue #1907 targets, the degenerate shapes a hand-edited row could hold (no
`provider` object, a scalar, a JSON-`null` section), and a `provider` carrying a second section
beside the matching one.

This is the rule the query implements and the one ADR-0579 records; the `Design` section
below derives both halves of the predicate from it.

## Design

One new module, `src/kdive/admin/profile_kinds.py`, and one new subcommand registered in
`src/kdive/__main__.py`. The module mirrors `src/kdive/admin/projects.py`'s split: an impure reader,
a pure formatter, and a dataclass between them.

### `ProfileKindMismatch`

```python
@dataclass(frozen=True, slots=True)
class ProfileKindMismatch:
    system_id: UUID
    project: str
    state: str
    profile_section: str
    resource_kind: str
```

`state`, `profile_section`, and `resource_kind` are `str`, not `SystemState` / `ResourceKind`.
This is a residue sweep: parsing a stored value into an enum is a way for the sweep to raise on
exactly the data it exists to find. The column `CHECK` constraints already bound the vocabulary.

`profile_section` is the rendered label for what the row actually holds — see below.

### The query

```sql
SELECT s.id            AS system_id,
       s.project       AS project,
       s.state         AS state,
       r.kind          AS resource_kind,
       s.provisioning_profile -> 'provider' AS provider_section
FROM systems AS s
JOIN allocations AS a ON a.id = s.allocation_id
JOIN resources AS r ON r.id = a.resource_id
WHERE jsonb_typeof(s.provisioning_profile -> 'provider' -> r.kind) IS DISTINCT FROM 'object'
   OR s.provisioning_profile -> 'provider'
        IS DISTINCT FROM jsonb_build_object(r.kind, s.provisioning_profile -> 'provider' -> r.kind)
ORDER BY s.created_at, s.id
```

A System is clean only when its stored `provider` is **exactly** `{<the bound kind>: {…}}`. The
first disjunct says the section under the bound kind is not an object; the second says the
`provider` object holds anything besides that one section. The second is not belt-and-braces:
without it, `{"fault-inject": {}, "local-libvirt": {}}` on a fault-inject Resource passes as
clean, and that row is worse than the residue this sweep targets — `_require_exactly_one_provider`
(`src/kdive/profiles/provisioning.py:306-315`) makes it fail `ProvisioningProfile.parse` outright,
so it breaks every parse site rather than the four lanes above.

The predicate is **total**: measured on PostgreSQL 17.10, `jsonb -> text` returns SQL `NULL` for a
missing key and for every non-object left operand (array, string scalar, JSON `null`), and
`jsonb_typeof(NULL::jsonb)` is `NULL`. Run over ten seeded profile shapes on a real three-table
join it returns the eight wrong ones — mismatched key, both two-key shapes, JSON-`null` section,
empty object, scalar, array, absent `provider` — and neither correct one. No System row can fall
out of the scan unreported, which is the property that makes a clean result mean something.

Totality is about **rows**, and the bound is worth stating because a reader will otherwise take it
as a statement about stored profiles being sound. The sweep compares the section **key** against
the bound kind and checks that the section is a JSON object. It does not validate the section
**body**, so a clean report does not imply every stored profile parses:
`{"provider": {"fault-inject": {}}}` on a fault-inject Resource is clean here — measured — and
`ProvisioningProfile.parse` rejects it with a `CategorizedError`. Detecting invalid section bodies
is outside #1907, whose criterion is the section key not equalling the kind.

Totality rests on the **join** too, and that half is a dependency rather than a proof.
`allocations.resource_id` is nullable — `0016_pending_queue.sql` dropped the `NOT NULL` and
`0017_queue_terminal_null_resource.sql` guards it with
`CHECK (resource_id IS NOT NULL OR state IN ('requested', 'released', 'failed'))` — so an inner
join would silently drop a System whose Allocation carried none. It holds today because
`systems.allocation_id` is `NOT NULL`, a System is minted only against a placed Allocation, and
no path NULLs `resource_id` afterwards (`rg -n "SET resource_id|resource_id = NULL" src/` finds
nothing). The query is deliberately **not** widened to a `LEFT JOIN` for a case nothing can
reach — that would add a NULL branch to `resource_kind`, a reported field, to cover a row no
code can construct. Naming the dependency is the fix.

Nothing in this design signals a relaxation at run time, and the residual is accepted unsignalled rather than unsignallable — ADR-0579 records the printed-count alternative and why it is not built.

The section **key** is not extracted in SQL. `jsonb_object_keys` fails with `cannot call jsonb_object_keys on a scalar` for a non-object argument, and a `jsonb_typeof`
`CASE` guard around a scalar sub-`SELECT` does hold in practice — but the deciding ground is
testability: the row carries the whole `provider` value back and Python renders the label,
which gets a case per stored shape where the SQL form gets none, on the one part of the sweep
whose job is to survive malformed data.

`ORDER BY s.created_at, s.id` makes the report stable across runs; `id` breaks ties on rows sharing
a timestamp.

### Rendering the observed section

```python
def _section_label(provider: object) -> str
```

Pure, and total over what psycopg can hand back for a `jsonb` column. Two rules, applied in
order:

```python
# Derived from the enum, never a literal: `resources.kind` has been widened twice already
# (0018, 0020), and both migrations say they mirror ResourceKind. A literal would not move
# with a fourth kind, so the report would render that kind `<unrecognized>` — hiding the one
# field criterion 2 asks for, in exactly the window a residue sweep exists for.
_KNOWN_KINDS = {kind.value for kind in ResourceKind}


def _section_label(provider: object) -> str:
    if provider is None:
        return "<none>"
    if not isinstance(provider, dict):
        return "<not-an-object>"
    if not provider:
        return "<none>"
    return ",".join(sorted({_entry(k, v) for k, v in provider.items()}))


def _entry(key: object, value: object) -> str:
    name = key if isinstance(key, str) and key in _KNOWN_KINDS else "<unrecognized>"
    return name if isinstance(value, dict) else f"{name}=<not-an-object>"
```

`_entry` takes `key: object` and narrows with `isinstance(key, str)`, and `_section_label`
iterates `items()` rather than indexing, because **the obvious form does not type-check**:
`isinstance(provider, dict)` narrows to `dict[Unknown, Unknown]`, so the keys are `object` and
both `_entry(key: str, …)` and `provider[key]` are rejected. Measured with `uv run ty check`, the
command `just type` runs: two diagnostics, exit 1 — Task 1 would go red on its first guardrail
run. The form above exits 0 and is behaviour-identical over 15 measured stored shapes covering
every row of the table below,
the 500-key bound included. The `isinstance(key, str)` is not dead defensiveness: it is what
makes the narrowing sound, and a sweep whose job is surviving malformed data should not assume
its own key type.

The `set` is load-bearing, not tidying. Entries are drawn from a closed pool — each known kind
in one of two forms, plus `<unrecognized>` in one of two forms — so deduplicating bounds the
label to at most five entries whatever the stored object holds. Measured: a `provider` carrying
500 unknown keys renders `<unrecognized>`, 14 characters. Without the `set` the label is linear
in key count, and one hand-edited row can emit an arbitrarily long line into a terminal.

Measured against `postgres:17` through the project venv:

| stored `provider` value | psycopg gives | label |
|---|---|---|
| one-key object `{"local-libvirt": {"a": 1}}` | `dict` | `local-libvirt` |
| two sections `{"fault-inject": {}, "local-libvirt": {}}` | `dict` | `fault-inject,local-libvirt` |
| 500 unknown keys | `dict` | `<unrecognized>` — deduplicated, 14 chars |
| **section not an object** `{"fault-inject": null}` (also `[]`, `"x"`) | `dict` | `fault-inject=<not-an-object>` |
| **unrecognized key** `{"\x1b[31mBOOM": {}}` | `dict` | `<unrecognized>` |
| empty object `{}` | `dict` | `<none>` |
| JSON `null` | `None` | `<none>` |
| `provider` key absent | `None` | `<none>` |
| string scalar `"nope"` | `str` | `<not-an-object>` |
| array `[1, 2]` | `list` | `<not-an-object>` |
| number `7` / bool `true` | `int` / `bool` | `<not-an-object>` |

Two rows in that table are load-bearing:

- **The section-not-an-object row is what the predicate's first disjunct exists to catch**, and a
  label rendering only the *key* would print `profile_section=fault-inject
  resource_kind=fault-inject` — two identical strings under a header saying these rows are
  mismatched, which reads as a broken sweep. `=<not-an-object>` is what makes the report legible
  for the case the first disjunct was written for.
- **The unrecognized-key row is what makes the vocabulary claim true.** Mapping any key outside
  `_KNOWN_KINDS` to a marker keeps the printed label closed over the three kind names and three
  markers whatever the stored object holds, which is what the threat model's control 3 asserts,
  and it is what bounds the label's *length* — 500 unknown keys render 14 characters.

  It is deliberately **not** justified as a defence against hostile keys. `dump_profile` is on
  both write paths and each section's alias *is* a `ResourceKind` value, so a key outside that
  vocabulary can only arrive by direct database write — and the threat model's item 4 already
  places that actor out of reach, since they can equally rewrite the report's inputs. Claiming
  the marker bounds a threat there would contradict it. The `project` control is different and
  its chain does hold: that value comes from an IdP claim validated only for non-empty-string.

JSON `null` and an absent `provider` are **indistinguishable** at the Python layer — both arrive
as `None` — so mapping both to `<none>` is forced, not merely convenient.

The markers are angle-bracketed so they cannot collide with a real `ResourceKind` value. A stored
key could itself be the literal string `<none>`, but it is not in `_KNOWN_KINDS`, so it renders
`<unrecognized>` and the ambiguity does not arise.

The rendered label is a **refinement beyond criterion 2**, which the raw section value would
already satisfy. It earns the refinement twice over: `LibvirtProfile.domain_xml_params` is an
agent-supplied `dict[NonEmptyStr, NonEmptyStr]`, so printing the raw `provider` value would put
caller-controlled text into an operator report; and the raw value cannot distinguish the two
rows called out above.

### Entry points

```python
async def scan_profile_kinds(conn: AsyncConnection) -> list[ProfileKindMismatch]
async def verify_profile_kinds() -> list[ProfileKindMismatch]
def format_profile_kind_result(
    mismatches: Sequence[ProfileKindMismatch], *, redacted_url: str
) -> str
```

`scan_profile_kinds` takes an open connection, so a test drives it against the migrated fixture
database with no pool lifecycle. `verify_profile_kinds` opens a pool with `create_pool()` and
delegates — an unset `KDIVE_DATABASE_URL` raises `CONFIGURATION_ERROR` there, before any query,
exactly as `verify_project` does. The pool is closed in a `finally`.

`format_profile_kind_result` is pure and returns the message. **The command always exits `0`;**
the printed report is the answer.

- **clean** → one line naming the credential-redacted target:
  `verified no System's provisioning-profile provider section mismatches its Resource kind in <url>`
- **mismatches** → a header, then one line per mismatch, then a closing block. The header is
  fixed text like every other line, so no later edit can drift it while the suite stays green:
  `found <n> System(s) whose provisioning-profile provider section does not match their Resource kind in <url>`
  then one line per mismatch:
  `system=<uuid> project=<project!r> state=<state> profile_section=<section> resource_kind=<kind>`
  — `project` rendered with `repr`, because it is the one field nothing else bounds (threat
  model, control 3); the other four are printed as they stand,
  then a closing block that **describes the class without asserting one cause**: each listed
  System's stored provider section does not match its bound Resource kind; a plain kind mismatch
  reaches `ready` and raises at first use on the control, install, boot-evidence, and vmcore
  lanes, while a section that fails `ProvisioningProfile.parse` outright — a `provider` holding
  two sections, none, or a section under the bound kind that is not an object — breaks *every*
  parse site instead; a row whose `state` is `torn_down` or `failed` is inert, since nothing
  runs against it, so it needs no action; and remediation is not automated.
  ADR-0549 is named as background for the kind-mismatch case, not as a claim about when each row
  was written.

That closing block describes the class and distinguishes the two blast radii rather than
asserting one cause, because no fixed wording is true of all four reported classes.

There is no non-zero exit because there could not be a working one. #1907's criteria ask for a
query, a report, no mutation, and a test — none asks for an exit code — and the sweep reports
every state, so a torn-down mismatched System would hold a non-zero exit red permanently with
nothing left to repair. A signal that cannot return to green is not a signal. (Operator
decision, 2026-08-25; see ADR-0579.)

The target URL passes through the existing `redact_database_url` from `kdive.admin.projects`; it
is not reimplemented.

### CLI wiring

`src/kdive/__main__.py` gains `_handle_verify_profile_kinds` and a `_Command` entry:

```python
_Command(
    "verify-profile-kinds",
    "report Systems whose stored profile section does not match their Resource kind",
    _handle_verify_profile_kinds,
)
```

No arguments — the sweep is whole-database by construction, and every filter that could be offered
(by project, by state) is the triage call #1907 excludes. The command is **not** `runnable`, so it
takes the same non-telemetry path as `verify-project`.

The handler follows `_handle_verify_project`'s shape — run the coroutine, format, `print` — and
stops there. It raises no `SystemExit`, so the command returns `0`; a `CategorizedError` from
`create_pool()` still routes through `main()`'s handler to the category's exit code (ADR-0089),
which is a configuration failure rather than a finding.

## Failure modes

| Condition | Behavior |
|---|---|
| `KDIVE_DATABASE_URL` unset or unusable | `create_pool()` raises `CategorizedError(CONFIGURATION_ERROR)`; `main()`'s handler prints it and exits with the category's code (ADR-0089) |
| database reachable, no mismatch | exit `0`, one clean line |
| database reachable, N mismatches | exit `0`, header + N lines + remediation line |
| a System whose `provider` is absent, scalar, or JSON-`null` | reported, with the label from the table above |
| a System whose `provider` carries a second section beside the matching one | reported; the label is both keys, sorted and comma-joined |
| a System whose section under the bound kind is not an object (`null`, array, scalar) | reported; the label is `<kind>=<not-an-object>`, never the bare kind name |
| a System whose `provider` carries a key outside the three `ResourceKind` values | reported; that key renders `<unrecognized>`, so its bytes never reach the terminal |
| a System bound to an Allocation with a NULL `resource_id` | **not reported** — the inner join drops it. Accepted residual, stated in ADR-0579 and unreachable today (see the join dependency above) |

## Threat model

The change is security-relevant only in that it adds a CLI entry point that prints stored row
identities.

1. **Boundary inventory.** Two boundaries are *added*. The `verify-profile-kinds` argv path into
   a database read — inert, since the command takes no arguments. And **stdout into the
   operator's terminal**, which is the one that matters: stored row content crosses it through
   `print`, and a terminal interprets what it is handed. Nothing
   is widened — the query is a `SELECT` over three tables the same process already reads through
   `migrate`, `seed-project`, and `verify-project`. No network listener, no new file, no new
   environment variable.
2. **Actor model.** The actor is a local operator on the host where `KDIVE_DATABASE_URL` resolves
   — the same actor who can already run `python -m kdive migrate` and `psql`. There is no
   untrusted caller **of the command**: it has no argument to influence and no remote surface, and
   anyone who can invoke it can already read the same rows directly.

   That says nothing about the **data** it prints, and the distinction is what control 3 turns on.
   The rows carry values two other sets of principals populate: `systems.project` comes from an
   IdP-issued `projects` claim whose only live-path validation rejects non-strings and empty
   strings (`security/authz/context.py:44-50` — no charset bound), and
   `LibvirtProfile.domain_xml_params` is agent-supplied. So printed bytes can be planted with no
   database access at all — remotely, not self-inflicted.

   That path has **one more hop than the claim above suggests**, and naming it keeps this
   inventory honest. `systems.project` is `alloc.project` (`services/systems/admission.py:651`),
   so a System exists only behind a placed Allocation, and Allocation admission is fail-closed on
   funding — `has_quota` records that "a project with **no quota row** is over quota (ADR-0007
   §4 — no silent default)" (`services/allocation/admission/core.py:605-611`). The hostile
   project string must therefore already own `budgets`/`quotas` rows, which come either from the
   operator running `seed-project` or from `accounting.set_budget` / `set_quota`, both gated by
   `require_role(ctx, project, Role.ADMIN)` (`mcp/tools/accounting/admin.py:53-54`). The planter
   is an ADMIN-role principal on a project whose *name* carries the hostile bytes — still remote,
   still needing no database access, still unvalidated as a string, but a materially narrower
   actor than "anyone holding a token with an arbitrary claim".

   This design trusts the command's caller and does **not** trust the row contents; the `repr`
   rendering and the closed label vocabulary are what carry that second half.
3. **Control per boundary.** The single statement is a static `LiteralString` with no parameters
   and no interpolation, so injection has no vector. The target database is printed through
   `redact_database_url`, which masks a URL password and blanket-redacts a keyword/value conninfo
   mentioning `password`. What is printed is `(system id, project, state, section label, resource
   kind)` — never the profile body, which is where a caller-supplied value could live.

   **Four of the five fields are bounded by something other than this code.** `system_id` is a
   `uuid`; `state` and `resource_kind` carry `CHECK` constraints (`systems_state_check`,
   `resources_kind_check`). The section **label** is bounded here: `_section_label` maps any key
   outside `_KNOWN_KINDS` to `<unrecognized>`, so the printed vocabulary is closed over the
   `ResourceKind` values and three markers even for a hand-edited row whose keys carry arbitrary
   bytes — in charset *and* in length, at most five entries however large the stored object is.

   `project` is the exception and needs its own control. `systems.project` is `text NOT NULL`
   with **no** `CHECK` (`0001_init.sql:57`), `System.project` is a bare `str` on `Attribution`,
   and the only live-path validation is a non-empty `isinstance(..., str)` test over the IdP
   claim (`security/authz/context.py:44-50`) — no charset bound anywhere. So the exact escape the
   label machinery exists to stop walks through the adjacent field on the same line.

   **`project` is rendered with `repr`** — `f"project={mismatch.project!r}"` — and that single
   operator is the whole control. It does three things a hand-rolled pair of them does not:

   - **Charset.** `repr` escapes exactly the characters `str.isprintable()` rejects. Measured:
     `\x1b`, `DEL` (U+007F), the C1 controls including `CSI` (U+009B), `NBSP` (U+00A0) and
     `RIGHT-TO-LEFT OVERRIDE` (U+202E) all come back escaped, and the rendered line is
     `isprintable()`. A `ord(c) < 0x20` bound would pass every one of those but the first, since
     they all sit above `0x20` — and a terminal honouring C1 reads a bare U+009B as the Control
     Sequence Introducer, which is the escape-sequence injection this control exists to stop.
   - **Tokenization.** It delimits the value *and* escapes any quote inside it. Hand-written
     quotes do not: a double quote is printable, so `project="…"` is closed by the next literal
     `"` the formatter emits rather than by the end of the value, and a project named
     `x" state=ready profile_section=fault-inject … junk="y` forges a full set of leading
     `key=value` pairs with the true values trailing them. Measured against the specified format.
     `repr` renders that same value as one token.
   - **Losslessness.** It escapes where a substitution helper destroys. Replacing a hostile byte
     with `?` silently rewrites a project name in a report an operator reads to decide what to do
     about that row.

   It is also already the idiom for this exact field: `format_verify_result` prints
   `project {project!r}`. `str.isprintable()` itself is the repo's guard idiom at
   `security/artifacts/bpf_filter.py:27`, `profiles/provisioning.py:66`, `jobs/payloads.py:211`,
   `domain/labels.py:57` and three more — but every one of those *rejects* where this must
   *render*, which is why the control is `repr` rather than a validator.

   The label needs no such treatment: it is closed over printable ASCII by construction, so
   putting it through the same operator would add a mechanism with no input that reaches it.

   **`project`'s bound is charset and tokenization, not length.** `repr` is a per-character
   escape and a delimiter; neither caps length, and `systems.project` is unbounded `text`.
   Measured against a realistic row (`state=ready`, `profile_section=local-libvirt`,
   `resource_kind=fault-inject`): a 5000-character project renders a 5123-character line. Accepted rather than fixed —
   this is a report an operator runs by hand and reads, not a fixed-width surface, and a width
   policy plus its tests costs more than the over-long line it prevents. Stated because the
   adjacent field *is* length-bounded and the asymmetry would otherwise read as an oversight.

   On failure, the `CategorizedError` path in `main()` renders through `Redactor`.
4. **Explicitly out of scope.** Access control on the command itself: the DB URL is the
   credential, matching every other `python -m kdive` operator subcommand, and adding an RBAC gate
   would require the MCP surface ADR-0579 rejects. **Audit trail:** the same cross-project read
   served over MCP would owe a `platform_auditor` gate and one `platform_audit_log` row
   (`src/kdive/mcp/tools/ops/_reads.py`); this command owes neither and records nothing. Accepted
   as a stated residual in ADR-0579 — there is no authenticated actor to attribute a row to, and
   the holder of the URL can already read the rows directly. Denial of service from a large
   `systems` table: the query is a **full scan** of `systems` — the predicate correlates
   `provisioning_profile` to `r.kind`, so it is not sargable and no index can serve it, and the
   plan demotes it to a per-row join filter. Measured at **~120-155 ms over 200k rows** — 200k
   each of `systems`, `allocations` and `resources`, all-clean one-key profiles, PostgreSQL 17.11
   in a stock `postgres:17` container on a Fedora developer host, warm, 2 parallel workers. That
   band is the spread of **three independent runs** on this host: 121.8 / 125.4 / 121.5 ms,
   129–138 ms, and 143.3 / 144.8 / 132.6 / 148.9 ms wall-clock, with `EXPLAIN (ANALYZE, BUFFERS)`
   Execution Time between 133.6 and 156.4 ms. A range rather than a figure is the honest form:
   three runs of the same query on the same host varied by 25%, so a single number would invite
   reliance it cannot carry. The **plan shape** is not host-dependent and reproduced exactly in
   every run. Run by hand and not on a loop, which is why that scan shape is acceptable rather
   than irrelevant. Tampering with
   `systems.provisioning_profile` by someone with direct database write access: that actor can
   equally rewrite the report's inputs, and no read-side check bounds them.

## Testing

`tests/admin/test_profile_kinds.py`, using the existing `migrated_url` fixture already re-exported
by `tests/admin/conftest.py`.

Database-backed:

1. **The acceptance case (#1907's own).** Seed a `fault-inject` Resource, a granted Allocation on
   it, and a `ready` System whose stored profile carries a `local-libvirt` section. Seed a second,
   matching System on a `local-libvirt` Resource. `scan_profile_kinds` returns exactly one row,
   and its five fields are the seeded values.

   **Run it read-only, and prove the guard is live.** This is the only bite behind criterion 3 —
   "the sweep reports rather than mutates" — which otherwise rests on inspection alone. ADR-0579
   anticipates a follow-up reusing `scan_profile_kinds` unchanged, so the contract needs a guard
   that outlives this change.

   **Open the connection as `await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)`
   — the `autocommit=True` is a requirement, not a style choice**, and the guard below is correct
   *because* of it. `AsyncConnection.connect()` defaults to `autocommit=False`, so an implementer
   who opens it the psycopg-default way walks into a dead guard. Measured on `postgres:17` (17.11)
   through the project venv, on the async connection this test actually opens:

   | connection | `SET default_transaction_read_only = on` | `SET TRANSACTION READ ONLY` |
   |---|---|---|
   | `autocommit=True` | `transaction_read_only=on`, write raises `ReadOnlySqlTransaction` | silent no-op, **write succeeds** |
   | `autocommit=False` | `transaction_read_only=off`, **write succeeds** | `transaction_read_only=on` |

   The two forms **swap correctness across the two modes**, because a `SET` issued inside an
   already-open transaction does not affect that transaction's read-only mode. So pin the mode and
   then the form: `autocommit=True` plus `SET default_transaction_read_only = on`, which is
   session-level and applies to the implicit transaction of every later statement.

   An earlier draft justified the choice by pointing at `tests/db/conftest.py`'s helpers using
   autocommit. That ground is unsound — those helpers are **synchronous** `psycopg.connect(...)`,
   and this test opens an `AsyncConnection`, whose default is the opposite. The requirement is
   stated here instead of inferred from a neighbouring pattern.

   Seed first, **then** issue the `SET`: the seeding writes and the `created_at` UPDATEs share
   this connection, so setting it earlier fails the fixture rather than the assertion.

   Then assert the guard itself: after the scan, a trivial `INSERT` on that same connection must
   raise `ReadOnlySqlTransaction`. Without that assertion the test passes whether the guard is
   live or dead — `scan_profile_kinds` only `SELECT`s either way — and criterion 3 reverts to the
   inspection-only state this item exists to escape.
2. **A clean database returns an empty list**, so criterion 1's assertion is not vacuously true —
   the same seed minus the mismatched System.
3. **Totality — no `provider` key.** A System whose stored `provisioning_profile` carries no
   `provider` key at all is reported, with `profile_section == "<none>"`. This is the property
   that makes an empty result trustworthy.
4. **Totality — a second section beside the matching one.** A System on a `fault-inject` Resource
   whose `provider` is `{"fault-inject": {...}, "local-libvirt": {...}}` is reported, with
   `profile_section == "fault-inject,local-libvirt"`. This is the regression guard for the
   predicate's second disjunct: with only the first, this row reads as clean, and it is a row
   `ProvisioningProfile.parse` rejects outright.
5. **Totality — the section under the bound kind is not an object.** A System on a `fault-inject`
   Resource whose `provider` is `{"fault-inject": null}` is reported, with
   `profile_section == "fault-inject=<not-an-object>"`. This is the **only** test in the suite
   that reddens if the predicate's *first* disjunct is dropped: tests 1–4 all mismatch on the key,
   which the second disjunct catches by itself. Without this case the first disjunct is untested
   despite being the half whose totality the spec argues at length.
6. **Order is deterministic.** Two halves, and the first one needs a mechanism the repositories
   deliberately withhold.

   `created_at` is in `_SERVER_GENERATED` (`db/repositories.py:53`), so it is excluded from
   `SYSTEMS._insert_columns` and `SYSTEMS.insert` **cannot** write it — the column falls to its
   schema default, transaction-start `now()`. Verified:
   `'created_at' in SYSTEMS._insert_columns` is `False`. So the row *content* comes from the
   repositories as everywhere else, and this one column is then imposed out of band: after
   inserting, issue `UPDATE systems SET created_at = %s WHERE id = %s` per row.

   **The fixture must disagree with the asserted order in BOTH of its orderings**, and that is
   one clause rather than an argument about which one the scan follows:

   - **insert** the tied pair with the larger-`id` row first, and
   - **`UPDATE`** every row in the reverse of the asserted order — the row that must come back
     last is updated first.

   Why both. `created_at` is server-generated, so the fixture imposes it with an out-of-band
   `UPDATE` after inserting (below), and PostgreSQL writes a new tuple version on `UPDATE`, so a
   seq scan over **that table** returns rows in last-write order — measured, 24 of 24 `UPDATE`
   permutations. That measurement is single-table and does **not** govern this sweep: the shipped
   query is a three-table join, whose un-ordered emission order is planner-dependent. Measured
   again during the build: a bare `SELECT id FROM systems` returns a different sequence from the
   join and fails against a correct implementation. So the fixture's precondition is asserted
   through the real query minus its `ORDER BY`, never through a single-table read. That makes `UPDATE` order the variable to control — but
   controlling only it leaves the fixture depending on an insertion order this document would
   then have to state and keep true. Fixing both costs one clause and removes the dependency.

   **The mutation this defends is silent.** If the fixture's order happens to match the asserted
   order, the scan returns it for free, and both `ORDER BY` mutations — dropping `, s.id`, and
   dropping the clause entirely — report green against a query that ships no `ORDER BY` at all.
   A correct implementation still passes either way, because the shipped
   `ORDER BY s.created_at, s.id` fully determines the result; what is lost is the mutation
   sensitivity that is the only reason this test imposes `created_at` out of band.

   One claim deliberately **not** made here: that a page prune returns the scan to insertion
   order. It was raised in review and did not reproduce — `VACUUM` and `ANALYZE` after the
   reverse-order `UPDATE`s left the scan in `UPDATE` order in all eight fixtures tried, including
   the two the claim named (PostgreSQL 17.11, psycopg 3.3.4, autocommit). The both-orders fixture
   above is adopted because it removes a dependency on an unstated variable, not because pruning
   was shown to reorder.

   - *`created_at` half:* one row carries the earlier timestamp, one the later; assert the
     earlier comes back first.
   - *`id` tiebreak half:* seed the tied pair with **fixed, explicit UUIDs** sharing one
     timestamp. Fixed UUIDs are load-bearing — every seed helper in this repo uses `uuid4()`,
     which would make this assertion pass or fail at random, and a flake that passes on re-run is
     not evidence.

   **The tie must be imposed by that same `UPDATE`, with one identical literal timestamp for both
   rows.** Do not rely on two rows seeded together sharing `now()`: measured, that holds inside an
   explicit transaction and is **false** on an `autocommit=True` connection — two `SELECT now()`
   calls returned timestamps 464 µs apart — and `autocommit=True` is the connection pattern item 1
   specifies, because it is what every helper in `tests/db/conftest.py` uses. Without a real tie
   the pair no longer ties, `ORDER BY s.created_at` alone fully determines their order, and the
   tiebreak assertion passes against an implementation carrying no `, s.id` at all.

There is deliberately **no** test guarding the join dependency. A comparison of the join's row
count against the fixture's `systems` count would look protective and detect nothing: a migration
relaxing the `allocations.resource_id` CHECK adds no such row to a fixture the test seeds, so both
counts stay equal and the suite stays green through exactly the change worth catching. The
dependency is an accepted residual, stated in ADR-0579 and in the failure-mode table above.

Pure:

7. `_section_label` over every row of the measured table above, including the
   section-not-an-object and unrecognized-key rows.
8. **Nothing unprintable reaches the printed line, and `project` cannot forge a field.** Two
   properties, one per half of control 3, and each needs its own assertion.

   *Charset.* `_section_label({"\x1b[31mBOOM": {}})` returns `<unrecognized>`; and a mismatch
   whose `project` carries a hostile byte renders through `repr`. **Assert the property, not the
   range:** `assert line.isprintable()`, never `all(ord(c) >= 0x20 for c in line)`. The second is
   the implementation restated as a test, and it cannot redden on the gap that matters — a
   `project` of `"a\x9b31mb"` satisfies it while `line.isprintable()` is `False`. Parametrize
   over `\x1b`, `\x7f` (DEL), `"\x9b"` (CSI), `"\xa0"` (NBSP) and `"\u202e"` (RLO), so the C1 and
   BiDi cases a `< 0x20` bound would have let through each get their own row. Assert on the
   **whole line**, not the label alone: `project` is the unbounded field, and a test covering
   only the label is how a sanitized-label/unsanitized-project pairing survives.

   *Tokenization.* The detector is the **anchor's presence**, and it must be asserted:

   ```python
   anchor = f"project={mismatch.project!r}"
   assert anchor in line  # this is the bite
   tail = line.split(anchor, 1)[1]
   ```

   A formatter that does not render `project` through `repr` — the hand-quoted form this control
   replaces, or a bare `project={project}` — puts no such token on the line, and the assertion
   fails with a legible message. Without it the split raises a bare `IndexError` instead, and the
   natural way to "repair" a test that errors is to guard the split, which makes it vacuous.

   **The count assertion that follows is documentation, not the bite.** Asserting `tail` carries
   exactly one `state=`, one `profile_section=` and one `resource_kind=` is a *constant*: once the
   anchor matches, the remainder of the line is the fixed suffix, so the counts are `(1, 1, 1)`
   for every possible input. Measured over 16 hostile values it failed zero times against a
   correct implementation. Keep it if it reads well; do not describe it as what catches the
   defect. (An earlier draft asserted `line.count("state=") == 1` over the *whole* line, which is
   vacuous for the opposite reason — `repr` escapes and keeps the hostile text, so `state=`
   appears twice on the fixed line and twice on the broken one.)

   **An input discriminates only if it does not contain `'` without also containing `"`.**
   Measured against the hand-quoted formatter:

   | `project` contains | broken formatter | correct | discriminates |
   |---|---|---|---|
   | neither quote | red — anchor absent | green | yes |
   | `"` only | red — anchor absent | green | yes |
   | **`'` only** | **green** | green | **NO** |
   | both | red — anchor absent | green | yes |

   The `'`-only row is the trap: `repr` then picks `"` as its own delimiter, so the broken line is
   byte-identical to the anchor and `line.isprintable()` passes too — green against a formatter
   that forges. **Do not add the single-quote sibling of the forgery case to this set as if it
   were a guard.**

   Parametrize over `'x state=ready profile_section=fault-inject'` and
   `'x" state=ready profile_section=fault-inject resource_kind=fault-inject junk="y'`. Both
   discriminate; the second reproduces the actual forgery, closing the field on its own `"` and
   emitting a full set of leading `key=value` pairs while `line.isprintable()` still passes.

9. `format_profile_kind_result([])` returns one line naming the redacted URL.
10. `format_profile_kind_result([one, two])` returns one line per mismatch, each carrying all
    five fields; the message names ADR-0549, says remediation is not automated, and names the
    four raising lanes. **Assert the header too** — that it carries the count `2` and the
    redacted URL. Without it the header is the only line of the report no test constrains, and
    it is also the only one an implementer would otherwise invent.
Handler (not pure — see the prerequisites):

11. **The handler prints the report and raises no `SystemExit`.** Capture stdout and assert it
    contains the seeded System's id *and* the closing remediation text, then assert no
    `SystemExit` escaped. The positive half is what makes this bite: "raises no exception" alone
    is satisfied by a handler whose body is `pass`, which opens no pool, prints nothing, and goes
    green — the vacuous shape items 2 and 6 exist to avoid.

    Two prerequisites, because the handler follows `_handle_verify_project`, which calls
    `database_url()` for the redacted target before printing: `KDIVE_DATABASE_URL` must be set to
    any resolvable value (else `CategorizedError(CONFIGURATION_ERROR)` from `db/pool.py`), and
    `verify_profile_kinds` is patched to return the mismatch list, so the test does not need a
    live database.

Parser:

12. `build_parser().parse_args(["verify-profile-kinds"])` yields
    `command == "verify-profile-kinds"`, and the command is absent from `_RUNNABLE`.

Pool wrapper:

13. **`verify_profile_kinds()` itself runs against a real database.** Items 1–6 drive
    `scan_profile_kinds(conn)`, 7–10 are pure, item 11 patches `verify_profile_kinds` out, and
    item 12 is the parser — so without this item the one function in the module with a **resource
    lifecycle** is never executed. Its `finally: await pool.close()` and its pre-query
    `CONFIGURATION_ERROR` would both ship unguarded, and the first row of the failure-mode table
    would have no bite anywhere in the suite. A leaked pool on an exception path, or a wrapper
    that opens the pool before resolving the URL, would go green.

    `monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)`, then assert `verify_profile_kinds()`
    returns the same list `scan_profile_kinds` returns for item 1's seed. This mirrors
    `verify_project`, the function this module is modelled on, which has four database-backed
    tests calling the pool-opening wrapper itself (`tests/admin/test_bootstrap.py:239`, `:258`,
    `:272`, `:286`) rather than patching it out.

The seeds go through `RESOURCES.insert` / `ALLOCATIONS.insert` / `SYSTEMS.insert` from
`kdive.db.repositories`, the pattern `tests/mcp/lifecycle/test_systems_list.py:104-155` uses, so the
fixture rows are built by the same code production uses and cannot drift from the schema. **One
carve-out, and only one:** `created_at` is server-generated and the repositories exclude it from
their insert columns, so item 6 sets it with a direct `UPDATE` after inserting. Every other
column comes from the repositories.

The helpers are module-local, taking an `AsyncConnection`. `tests/integration/_seed.py` is not
reused: it is hardwired to local-libvirt through `LocalLibvirtDiscovery` and a fake libvirt
connection, and takes a pool, so it can seed neither a `fault-inject` Resource nor a deliberately
malformed profile. No shared helper is introduced either — 48 test modules call `SYSTEMS.insert`
directly and the only shared seeders in the tree are area-local.

## Out of scope

- Repairing, cordoning, or tearing down a mismatched System, and choosing between those — #1907
  states this explicitly.
- Any change to ADR-0549's write-path check, or to `control._op_opt_in`'s unguarded read-path
  parse, which ADR-0549 keeps total on purpose.
- Exposing the sweep over MCP or `kdivectl`, and scheduling it in the reconciler — both rejected in
  ADR-0579.
- A machine-readable (`--json`) output mode. Nothing in #1907 asks for one, and the report is for
  a human deciding what to do next.
