# Detect Systems whose stored profile section does not match their Resource kind

Issue: [#1907](https://github.com/randomparity/kdive/issues/1907)
Decision record: [ADR-0579](../../adr/0579-stored-profile-kind-sweep-is-an-operator-cli-read.md)
Builds on: [ADR-0549](../../adr/0549-profile-policy-declares-its-resource-kind.md),
[ADR-0256](../../adr/0256-onboard-target.md)

## Goal

Give an operator one command that reports every stored System whose provisioning-profile provider
section is not the kind of the Resource its Allocation is bound to, and exits non-zero when it
finds any. Report only — no row is written, and no remediation is chosen.

## Background

ADR-0549 added the admission cross-check that rejects a kind-mismatched profile at
`systems.provision` and `systems.reprovision`. It runs on the write path only; a System minted
before it keeps its mismatched profile, and the check never runs on a stored-profile read so that
`control._op_opt_in`'s unguarded parse cannot start raising on stored data.

A **fault-inject** Resource holding a libvirt-section profile was accepted outright before
ADR-0549 and reaches `ready`, because the fault-inject provisioner discards the profile. It then
raises a bare `AttributeError` from `ProviderSection.fault_inject` at first use, on
`src/kdive/mcp/tools/control/registrar.py`, `src/kdive/services/runs/steps.py`,
`src/kdive/mcp/tools/lifecycle/vmcore/handlers.py`, and
`src/kdive/mcp/tools/debug/sessions/lifecycle.py`. The two libvirt kinds could not reach `ready`,
so the expected residue is fault-inject Systems.

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

A System is **mismatched** when `provisioning_profile -> 'provider'` does not carry an object under
the bound Resource's `kind`. That covers the residue #1907 targets and, without a second rule, the
degenerate shapes a hand-edited row could hold (no `provider` object, a scalar, a JSON-`null`
section).

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
ORDER BY s.created_at, s.id
```

The predicate is **total**: measured on PostgreSQL 17.10, `jsonb -> text` returns SQL `NULL` for a
missing key and for every non-object left operand (array, string scalar, JSON `null`), and
`jsonb_typeof(NULL::jsonb)` is `NULL`, so `IS DISTINCT FROM 'object'` is true for all of them. No
System row can fall out of the scan unreported, which is the property that makes a clean result
mean something.

The section **key** is not extracted in SQL. `jsonb_object_keys` is a set-returning function that
fails with `cannot call jsonb_object_keys on a scalar` for a non-object argument, and a `CASE`
guard is not a documented guarantee against evaluating the other arm. The row carries the whole
`provider` value back and Python renders the label.

`ORDER BY s.created_at, s.id` makes the report stable across runs; `id` breaks ties on rows sharing
a timestamp.

### Rendering the observed section

```python
def _section_label(provider: object) -> str
```

Pure, and total over what psycopg can hand back for a `jsonb` column:

| stored `provider` value | label |
|---|---|
| one-key object, e.g. `{"local-libvirt": {...}}` | `local-libvirt` |
| multi-key object | the keys sorted and comma-joined |
| empty object `{}` | `<none>` |
| SQL `NULL` or JSON `null` | `<none>` |
| any other JSON type (array, string, number, bool) | `<not-an-object>` |

The markers are angle-bracketed so they cannot collide with a real `ResourceKind` value.

### Entry points

```python
async def scan_profile_kinds(conn: AsyncConnection) -> list[ProfileKindMismatch]
async def verify_profile_kinds() -> list[ProfileKindMismatch]
def format_profile_kind_result(
    mismatches: Sequence[ProfileKindMismatch], *, redacted_url: str
) -> tuple[str, int]
```

`scan_profile_kinds` takes an open connection, so a test drives it against the migrated fixture
database with no pool lifecycle. `verify_profile_kinds` opens a pool with `create_pool()` and
delegates — an unset `KDIVE_DATABASE_URL` raises `CONFIGURATION_ERROR` there, before any query,
exactly as `verify_project` does. The pool is closed in a `finally`.

`format_profile_kind_result` is pure and returns `(message, exit_code)`:

- **clean** → exit `0`, one line naming the credential-redacted target:
  `verified no System's provisioning-profile provider section mismatches its Resource kind in <url>`
- **mismatches** → exit `1`, a header naming the count and the target, then one line per mismatch:
  `system=<uuid> project=<project> state=<state> profile_section=<section> resource_kind=<kind>`,
  then a closing line stating that these Systems predate ADR-0549's admission cross-check, that
  they raise on the control, install, vmcore, and debug-session lanes, and that remediation is not
  automated.

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

The handler mirrors `_handle_verify_project` exactly: run the coroutine, format, `print`,
`raise SystemExit(code)`.

## Failure modes

| Condition | Behavior |
|---|---|
| `KDIVE_DATABASE_URL` unset or unusable | `create_pool()` raises `CategorizedError(CONFIGURATION_ERROR)`; `main()`'s handler prints it and exits with the category's code (ADR-0089) |
| database reachable, no mismatch | exit `0`, one clean line |
| database reachable, N mismatches | exit `1`, header + N lines + remediation line |
| a System whose `provider` is absent, scalar, or JSON-`null` | reported, with the label from the table above |
| an Allocation with no Resource row | impossible — `allocations.resource_id` is `NOT NULL REFERENCES resources (id)` |

## Threat model

The change is security-relevant only in that it adds a CLI entry point that prints stored row
identities.

1. **Boundary inventory.** One boundary is *added*: the `verify-profile-kinds` argv path into a
   database read. None is widened — the query is a `SELECT` over three tables the same process
   already reads through `migrate`, `seed-project`, and `verify-project`. No network listener, no
   new file, no new environment variable.
2. **Actor model.** The actor is a local operator on the host where `KDIVE_DATABASE_URL` resolves
   — the same actor who can already run `python -m kdive migrate` and `psql`. There is no
   untrusted caller: the command has no input to influence and no remote surface. Anyone who can
   invoke it can already read the same rows directly. This design places its trust exactly there
   and nowhere else.
3. **Control per boundary.** The single statement is a static `LiteralString` with no parameters
   and no interpolation, so injection has no vector. The target database is printed through
   `redact_database_url`, which masks a URL password and blanket-redacts a keyword/value conninfo
   mentioning `password`. What is printed is bounded to `(system id, project, state, section key,
   resource kind)` — never the profile body, which is where a caller-supplied value could live.
   On failure, the `CategorizedError` path in `main()` renders through `Redactor`.
4. **Explicitly out of scope.** Access control on the command itself: the DB URL is the
   credential, matching every other `python -m kdive` operator subcommand, and adding an RBAC gate
   would require the MCP surface ADR-0579 rejects. Denial of service from a large `systems` table:
   the query is a single indexed-join scan an operator runs by hand, not on a loop. Tampering with
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
2. **A clean database returns an empty list**, so criterion 1's assertion is not vacuously true —
   the same seed minus the mismatched System.
3. **Totality.** A System whose stored `provisioning_profile` carries no `provider` key at all is
   reported, with `profile_section == "<none>"`. This is the property that makes an empty result
   trustworthy.
4. **Order is deterministic** across two mismatched Systems with distinct `created_at`.

Pure:

5. `_section_label` over each row of the table above.
6. `format_profile_kind_result([])` → exit `0`, and the message names the redacted URL.
7. `format_profile_kind_result([one, two])` → exit `1`, one line per mismatch, each carrying all
   five fields, and the message names ADR-0549 and says remediation is not automated.

Parser:

8. `build_parser().parse_args(["verify-profile-kinds"])` yields `command == "verify-profile-kinds"`,
   and the command is absent from `_RUNNABLE`.

The seeds go through `RESOURCES.insert` / `ALLOCATIONS.insert` / `SYSTEMS.insert` from
`kdive.db.repositories`, the pattern `tests/services/test_allocation_enqueue.py` uses, so the
fixture rows are built by the same code production uses and cannot drift from the schema.

## Out of scope

- Repairing, cordoning, or tearing down a mismatched System, and choosing between those — #1907
  states this explicitly.
- Any change to ADR-0549's write-path check, or to `control._op_opt_in`'s unguarded read-path
  parse, which ADR-0549 keeps total on purpose.
- Exposing the sweep over MCP or `kdivectl`, and scheduling it in the reconciler — both rejected in
  ADR-0579.
- A machine-readable (`--json`) output mode. Nothing in #1907 asks for one, and the report is for
  a human deciding what to do next.
