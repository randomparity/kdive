# Implementation plan — detect Systems whose stored profile section does not match their Resource kind (#1907)

Derived from
[the spec](../specs/2026-08-25-profile-kind-residue-sweep-design.md) and
[ADR-0579](../../adr/0579-stored-profile-kind-sweep-is-an-operator-cli-read.md),
both hardened by adversarial review. **Do not re-open the decisions they record** — in
particular the exposure choice (an operator CLI read, not a `just` recipe, reconciler pass, or
MCP tool) and the always-`0` exit.

**Goal.** One read-only operator command, `python -m kdive verify-profile-kinds`, that reports
every stored System whose `provisioning_profile -> 'provider'` section key is not the kind of
the Resource its Allocation is bound to. Report only: no row is written and no remediation is
chosen.

**Architecture.** One new module `src/kdive/admin/profile_kinds.py` mirroring
`src/kdive/admin/projects.py`'s split — an impure reader, a pure formatter, and a frozen
dataclass between them — plus one `_Command` entry in `src/kdive/__main__.py`. Nothing else in
the tree changes: no schema change, no migration, no write path, no new dependency.

**Stack.** Python 3.14, psycopg 3.3.4 / psycopg-pool, PostgreSQL 17, pytest, `uv`, `just`.

## Global constraints

Transcribed from the spec and ADR — values exactly as written there.

- **The command always exits `0`.** The handler raises no `SystemExit`. A `CategorizedError`
  from `create_pool()` still routes through `main()`'s handler to the category's code
  (ADR-0089); that is a configuration failure, not a finding. Operator decision, 2026-08-25.
- **The SQL is a single static `LiteralString` with no parameters and no interpolation.** Copy
  it from the spec verbatim, including `ORDER BY s.created_at, s.id`.
- **Both `WHERE` disjuncts ship.** Dropping the second lets
  `{"fault-inject": {}, "local-libvirt": {}}` on a fault-inject Resource read as clean; dropping
  the first lets `{"fault-inject": null}` read as clean. Tests 4 and 5 are their respective
  regression guards and each is the *only* test that reddens for its disjunct.
- **`_KNOWN_KINDS` is derived from the `ResourceKind` enum, never a literal.** `resources.kind`
  has been widened twice already (`0018`, `0020`); a literal would render a fourth kind
  `<unrecognized>` in exactly the window a residue sweep exists for.
- **The `set` in `_section_label` is load-bearing.** It bounds the label to at most five entries
  whatever the stored object holds — 500 unknown keys render 14 characters. Without it one
  hand-edited row emits an arbitrarily long line into a terminal.
- **`state`, `profile_section`, and `resource_kind` are `str`, not enums.** Parsing a stored
  value into an enum is how the sweep raises on exactly the data it exists to find.
- **`redact_database_url` is imported from `kdive.admin.projects`, not reimplemented.**
- **Open the scan connection `autocommit=True`, then use
  `SET default_transaction_read_only = on`.** Both halves are requirements and the second is
  correct *because* of the first. `AsyncConnection.connect()` defaults to `autocommit=False`, and
  measured, the two `SET` forms **swap correctness across the modes**: under autocommit the chosen
  form raises `ReadOnlySqlTransaction` and `SET TRANSACTION READ ONLY` is a silent no-op; without
  autocommit the chosen form is dead and the rejected one works. Do not ground this on
  `tests/db/conftest.py`'s helpers — they are *synchronous*, and their default is the opposite of
  the async one.
- **Assert the property, not the implementation's range.** `assert line.isprintable()`, never
  `all(ord(c) >= 0x20 for c in line)`.
- **No test guards the join dependency.** A row-count comparison against a fixture the test
  seeds cannot fire; it is an accepted residual recorded in ADR-0579.
- Guardrail: `env -u FORCE_COLOR just ci`. Run gates bare — no `| tail`, no redirect, no
  `|| true`. In zsh the array is `$pipestatus`, 1-indexed.
- Conventional commits. Never commit to `main`. Never force-push.

## File map

| file | action | answerable for |
|---|---|---|
| `src/kdive/admin/profile_kinds.py` | create | the dataclass, the label renderer, the query, the formatter |
| `tests/admin/test_profile_kinds.py` | create | every behaviour below — pure, DB, and handler |
| `src/kdive/__main__.py` | modify | `_handle_verify_profile_kinds` and the `_Command` entry |
| `tests/test_main_version.py` | modify | the parser assertion (test 12) |

`tests/admin/test_profile_kinds.py` importing `kdive.admin.profile_kinds` by dotted path is what
satisfies `tests/guards/test_module_has_direct_test.py`; the module would otherwise fail that
guard. The module docstring cites ADR-0579, which is **Accepted**, so `adr-status-check`'s
Proposed-but-cited rule is not engaged.

`scripts/gen_cli_verbs.py` is **not** affected: it generates `kdivectl` verbs from live MCP tool
schemas, and a `python -m kdive` subcommand is not a tool. `cli-verbs-check` needs no
regeneration.

---

## Task 1 — the pure surface

**Files:** create `src/kdive/admin/profile_kinds.py`, create `tests/admin/test_profile_kinds.py`.

Everything in this task is pure and needs no database, so it runs in milliseconds and the whole
label/format contract is pinned before a container is ever started.

**Interfaces.**

```python
@dataclass(frozen=True, slots=True)
class ProfileKindMismatch:
    system_id: UUID
    project: str
    state: str
    profile_section: str
    resource_kind: str


def _section_label(provider: object) -> str
def _entry(key: object, value: object) -> str
def format_profile_kind_result(
    mismatches: Sequence[ProfileKindMismatch], *, redacted_url: str
) -> str
```

There is **no sanitizing helper**. `project` is rendered `f"project={mismatch.project!r}"` and
that one operator is the whole control: `repr` escapes exactly what `str.isprintable()` rejects,
delimits the value, and escapes any quote inside it. The label needs nothing — it is closed over
printable ASCII by construction.

Take `_section_label`, `_entry`, and `_KNOWN_KINDS` from the spec **verbatim** — the branch order
is load-bearing and each branch has a test below.

Copy the signatures exactly as the spec gives them, `_entry(key: object, …)` included. The obvious
`key: str` form does **not** type-check: `isinstance(provider, dict)` narrows to
`dict[Unknown, Unknown]`, so the keys are `object` and both `_entry(key: str, …)` and
`provider[key]` are rejected — measured with `uv run ty check`, the command `just type` runs, two
diagnostics and exit 1. The spec's form takes `key: object`, narrows with `isinstance(key, str)`,
and iterates `items()`; it exits 0 and is behaviour-identical over 15 measured stored shapes
covering every row of the
table, the 500-key bound included.

**Tests (spec items 7–10).**

1. `_section_label` over **every row** of the spec's measured table: one-key object, two
   sections, 500 unknown keys, section-not-an-object (`null`, `[]`, `"x"`), unrecognized key,
   empty object, `None` (both JSON `null` and absent `provider`), string scalar, array, number,
   bool. Parametrize; each row is a case.
2. **Charset — nothing unprintable reaches the printed line.** Assert on the **whole formatted
   line**, not the label alone: `assert line.isprintable()`. Parametrize the `project` input over
   `\x1b`, `\x7f` (DEL), `\x9b` (CSI), `\xa0` (NBSP), `\u202e` (RLO). A `< 0x20` assertion would
   pass on four of those five, which is why the property is asserted instead of the range.
3. **Tokenization — `project` cannot forge a field.** The assertion is anchored on the rendered
   token, never on a quote character:

   ```python
   assert f"project={project!r}" in line  # fail here, not on an IndexError below
   tail = line.split(f"project={project!r}", 1)[1]
   ```

   then assert `tail` carries exactly one `state=`, one `profile_section=` and one
   `resource_kind=`. The explicit `in line` assertion is load-bearing: without it a broken
   formatter fails with an incidental `IndexError` from the split rather than on an assertion,
   and the natural way to "repair" a test that errors is to guard the split — which makes it
   vacuous.

   **An input discriminates only if it does not contain `'` without also containing `"`.**
   Measured against the hand-quoted formatter this control replaces:

   | `project` contains | broken formatter | fixed | discriminates |
   |---|---|---|---|
   | neither quote | red — anchor absent | green | yes |
   | `"` only | red — anchor absent | green | yes |
   | **`'` only** | **green** | green | **NO** |
   | both | red — anchor absent | green | yes |

   The `'`-only row is the trap: `repr` then picks `"` as its own delimiter, so the broken line is
   byte-identical to the anchor, the split succeeds, the tail counts come back `(1, 1, 1)`, and
   `line.isprintable()` passes too — green against a formatter that forges. **Do not add the
   single-quote sibling of case 2 to this set as if it were a guard.**

   Parametrize over `x state=ready profile_section=fault-inject` (no quote) and
   `x" state=ready profile_section=fault-inject resource_kind=fault-inject junk="y`. Both
   discriminate; the second is the one that reproduces the actual forgery, closing the field on
   its own `"` and emitting a full set of leading `key=value` pairs while `line.isprintable()`
   still passes. Two earlier wordings were wrong and are recorded so they are not reintroduced:
   `line.count("state=") == 1` is vacuous (`repr` escapes and keeps the hostile text, so `state=`
   appears twice on the fixed line *and* twice on the broken one), and "slice at the closing
   quote" reddens the *correct* implementation (the delimiter `repr` picks is data-dependent, so
   no fixed character anchors the set).

4. `format_profile_kind_result([])` returns one line naming the redacted URL.
5. `format_profile_kind_result([one, two])` returns the fixed header carrying the count and the
   redacted URL, then one line per mismatch each carrying all five fields; the closing block
   names ADR-0549, says remediation is not automated, and names the four raising lanes. Assert
   the header explicitly — it is the only report line an implementer would otherwise invent.

**Mutation check before moving on.** Break `_entry`'s `isinstance(value, dict)` branch and
confirm the section-not-an-object case reddens; break the `key in _KNOWN_KINDS` branch and
confirm the unrecognized-key case reddens; restore and confirm green. Clear `__pycache__`
between runs — a same-size single-character mutation reverted within a second reuses cached
bytecode and inverts the verdict.

**Verify:** `uv run pytest tests/admin/test_profile_kinds.py`, `just lint`, `just type`.

---

## Task 2 — the query

**Files:** modify `src/kdive/admin/profile_kinds.py`, modify `tests/admin/test_profile_kinds.py`.

**Interface.**

```python
async def scan_profile_kinds(conn: AsyncConnection) -> list[ProfileKindMismatch]
```

Takes an **open connection**, not a pool, so a test drives it against the migrated fixture
database with no pool lifecycle. It executes the single static statement, maps each row through
`_section_label`, and returns the list in the order the query produced.

**Fixtures.** Use the `migrated_url` fixture (function-scoped, from `tests/db/conftest.py:319`,
already re-exported by `tests/admin/conftest.py`) and open the connection as
`await psycopg.AsyncConnection.connect(migrated_url, autocommit=True)`. The `autocommit=True` is a
**requirement**: `AsyncConnection.connect()` defaults to `autocommit=False`, and test 1's
read-only guard is dead in that mode (see the Global Constraints entry). Do not ground it on
`tests/db/conftest.py`'s helpers — those are *synchronous*, and their default is the opposite.

**Do not reach for `pg_conn`.** It is re-exported alongside `migrated_url`, but it is a
*synchronous* `psycopg.Connection` and its first act is
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (`tests/db/conftest.py:209-216`) — so it
yields an unmigrated database with no `systems` table, and the repositories this task mandates
take an `AsyncConnection`. Both sub-steps that invite a raw connection — the out-of-band
`UPDATE systems SET created_at`, and the read-only proof's trivial `INSERT` — must use the same
`AsyncConnection` as the scan.

Seeds go through `RESOURCES.insert` / `ALLOCATIONS.insert` / `SYSTEMS.insert` from
`kdive.db.repositories` — the pattern `tests/mcp/lifecycle/test_systems_list.py` uses (its
helpers span 102-170 and seed all three in that order) — so fixture rows are built by the code
production uses and cannot drift from the schema.
(`tests/services/test_allocation_enqueue.py` is *not* the model to copy: it seeds `RESOURCES`
and `ALLOCATIONS` only and never reaches `SYSTEMS.insert`.) Helpers are **module-local**, taking an
`AsyncConnection`; `tests/integration/_seed.py` is not reused (hardwired to local-libvirt through
`LocalLibvirtDiscovery`, takes a pool, and can seed neither a `fault-inject` Resource nor a
malformed profile), and no shared helper is introduced (48 test modules call `SYSTEMS.insert`
directly; the only shared seeders in the tree are area-local).

**Tests (spec items 1–6).**

1. **Criterion 1, and the read-only proof.** Seed one mismatched `ready` System — a
   `fault-inject` Resource whose stored `provider` is a `local-libvirt` section — plus clean
   rows, and assert the scan returns exactly the mismatched System with all five fields.

   Seed first, **then** issue `SET default_transaction_read_only = on` — the seeding writes and
   the `created_at` UPDATEs share this connection, so setting it earlier fails the fixture rather
   than the assertion. Run the scan under it, then **assert the guard is live**:
   after the scan, a trivial `INSERT` on that same connection must raise
   `psycopg.errors.ReadOnlySqlTransaction`. Without that assertion the test passes whether the
   guard is live or dead — `scan_profile_kinds` only `SELECT`s either way — and criterion 3
   reverts to inspection alone.
2. **A clean database returns an empty list** — the same seed minus the mismatched System, so
   test 1's assertion is not vacuously true.
3. **Totality — no `provider` key.** Reported, `profile_section == "<none>"`.
4. **Totality — a second section beside the matching one.** `{"fault-inject": {…},
   "local-libvirt": {…}}` on a fault-inject Resource is reported,
   `profile_section == "fault-inject,local-libvirt"`. Regression guard for the **second**
   disjunct.
5. **Totality — the section under the bound kind is not an object.** `{"fault-inject": null}` on
   a fault-inject Resource is reported,
   `profile_section == "fault-inject=<not-an-object>"`. The **only** test that reddens if the
   **first** disjunct is dropped — tests 1–4 all mismatch on the key, which the second disjunct
   catches by itself.
6. **Order is deterministic**, in two halves. **The fixture must disagree with the asserted
   order in BOTH of its orderings** — see the spec's item 6, which this restates.

   `created_at` is in `_SERVER_GENERATED` (`db/repositories.py:53`) so `SYSTEMS.insert` cannot
   write it. Impose it out of band after inserting:
   `UPDATE systems SET created_at = %s WHERE id = %s`. This is the **only** carve-out from the
   repositories in the whole fixture; every other column comes from them.

   Two requirements, both load-bearing:

   - **insert** the tied pair with the larger-`id` row first, and
   - **`UPDATE`** every row in the reverse of the asserted order — the row that must come back
     last is updated first.

   PostgreSQL writes a new tuple version on `UPDATE`, so a seq scan over a **single table**
   returns rows in last-write order (measured: 24 of 24 `UPDATE` permutations). That measurement
   does not govern this sweep — the shipped query is a three-table join whose un-ordered emission
   order is planner-dependent, and a bare single-table read returns a different sequence. Assert
   the fixture's precondition through the real query minus its `ORDER BY`. It still makes
   `UPDATE` order the variable to control, but controlling only it leaves the fixture depending
   on an insertion order nothing states. Fixing both costs one clause and removes the dependency.

   **The mutation this defends is silent.** If the fixture's order happens to match the asserted
   order, the scan returns it for free and both `ORDER BY` mutations report green against a query
   shipping no `ORDER BY` at all. A correct implementation still passes either way — what is lost
   is the mutation sensitivity that is the only reason this test imposes `created_at` out of band.

   - *`created_at` half:* one row carries the earlier timestamp, one the later; assert the
     earlier comes back first.
   - *`id` tiebreak half:* seed the tied pair with **fixed, explicit UUIDs**. Fixed UUIDs are
     load-bearing — every seed helper in this repo uses `uuid4()`, which would make this
     assertion pass or fail at random, and a flake that passes on re-run is not evidence.

   **Impose the tie with one literal timestamp in the same `UPDATE`.** Do not rely on two rows
   seeded together sharing `now()`: measured, that holds inside an explicit transaction and is
   **false** on the `autocommit=True` connection this task requires — two `SELECT now()` calls
   came back 464 µs apart. Without a real tie the pair does not tie, `ORDER BY s.created_at`
   alone fully determines their order, and the tiebreak assertion passes against an
   implementation carrying no `, s.id` at all.

**Mutation check before moving on.** Drop the second `WHERE` disjunct and confirm test 4 alone
reddens; drop the first and confirm test 5 alone reddens; drop `, s.id` from the `ORDER BY` and
confirm test 6's tiebreak half reddens; drop the whole `ORDER BY` and confirm both halves
redden. Restore and confirm green.

**Container hygiene.** Ryuk is disabled in this repo and the refcount is the only reaper, so an
interrupted run strands testcontainers forever and a green suite proves nothing about cleanup.
After the task, sweep with `docker ps -a --filter label=org.testcontainers=true` and remove
leftovers explicitly (issues #1910, #1911).

**Verify:** `uv run pytest tests/admin/test_profile_kinds.py`, `just lint`, `just type`.

---

## Task 3 — pool wrapper and CLI wiring

**Files:** modify `src/kdive/admin/profile_kinds.py`, modify `src/kdive/__main__.py`, modify
`tests/admin/test_profile_kinds.py`, modify `tests/test_main_version.py`.

**Interfaces.**

```python
async def verify_profile_kinds() -> list[ProfileKindMismatch]
```

Opens a pool with `create_pool()` and delegates to `scan_profile_kinds`; an unset
`KDIVE_DATABASE_URL` raises `CONFIGURATION_ERROR` there, before any query, exactly as
`verify_project` does. The pool is closed in a `finally`.

In `src/kdive/__main__.py`, `_handle_verify_profile_kinds` follows `_handle_verify_project`'s
shape — run the coroutine, format, `print` — and **stops there**, raising no `SystemExit`. The
registry entry carries no `add_arguments` and is **not** `runnable`:

```python
_Command(
    "verify-profile-kinds",
    "report Systems whose stored profile section does not match their Resource kind",
    _handle_verify_profile_kinds,
)
```

No arguments: the sweep is whole-database by construction, and every filter that could be
offered (by project, by state) is the triage call #1907 excludes.

**Tests (spec items 11–13).**

1. **The handler prints the report and raises no `SystemExit`.** Capture stdout and assert it
   contains the seeded System's id **and** the closing remediation text, *then* assert no
   `SystemExit` escaped. The positive half is what makes this bite — "raises no exception"
   alone is satisfied by a handler whose body is `pass`.

   Two prerequisites, because the handler follows `_handle_verify_project`, which calls
   `database_url()` for the redacted target before printing: `KDIVE_DATABASE_URL` must be set to
   any resolvable value (else `CategorizedError(CONFIGURATION_ERROR)` from `db/pool.py`), and
   `verify_profile_kinds` is patched to return the mismatch list, so the test needs no live
   database.
2. `build_parser().parse_args(["verify-profile-kinds"])` yields
   `command == "verify-profile-kinds"`, and the command is absent from `_RUNNABLE`.
3. **`verify_profile_kinds()` itself runs against a real database** (spec item 13). Without it the
   one function in the module with a **resource lifecycle** is executed by nothing: tests 1-6 of
   Task 2 drive `scan_profile_kinds(conn)`, Task 1 is pure, test 1 above patches
   `verify_profile_kinds` out, and test 2 is the parser. Its `finally: await pool.close()` and its
   pre-query `CONFIGURATION_ERROR` would both ship unguarded, and the first row of the spec's
   failure-mode table would have no bite anywhere in the suite.

   `monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)`, then assert `verify_profile_kinds()`
   returns the same list `scan_profile_kinds` returns for Task 2 test 1's seed. This mirrors
   `verify_project` — the function this module is modelled on — which has four database-backed
   tests calling the pool-opening wrapper itself (`tests/admin/test_bootstrap.py:239`, `:258`,
   `:272`, `:286`) rather than patching it out.

**Mutation check before moving on.** Replace the handler body with `pass` and confirm test 1
reddens on the stdout assertion, not merely on the `SystemExit` half. Add `runnable=True` to the
`_Command` and confirm test 2 reddens.

**Verify:** `uv run pytest tests/admin/test_profile_kinds.py tests/test_main_version.py`,
then the full gate: `env -u FORCE_COLOR just ci`.

---

## Out of scope

Repair or migration of mismatched rows; the cordon vs teardown vs operator-notification policy;
any change to ADR-0549's write-path check or to `control._op_opt_in`'s unguarded read-path
parse. All three are excluded by the frozen charter, owned by the issue body and ADR-0549's
consequences.

Validating the section **body**: the sweep compares the section key against the bound kind and
checks the section is an object, so `{"provider": {"fault-inject": {}}}` on a fault-inject
Resource is clean here and still rejected by `ProvisioningProfile.parse`. The spec states that
bound; the plan does not widen it.

A run-time signal for the join residual: a printed scanned/total count would warn, and is
declined as surface #1907 did not ask for.
