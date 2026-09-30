# Startup recovers a torn authority journal tail (#2983) — plan

Goal: startup removes a torn, never-anchored final journal line (preserving its bytes) instead
of refusing service; every other torn or corrupt lane still refuses.

Architecture: `FileAuthorityJournal` gains a recovery load that reports a torn final line and a
removal that preserves then truncates it; `host._retract_unanchored_tail` decides from the
trusted head, and `run_authority_host` also reconciles on `journal: invalid-lane`. Spec:
`docs/workflow/specs/2026-09-29-journal-torn-tail-2983-design.md`; ADR-0584 #2983 amendment.

Tech stack: Python 3.14, pytest, Postgres-backed `tests/db` fixtures.

Expected implementation size: 190–260 changed lines (M) — about 60 in `journal.py`, 25 in
`host.py`, 110 in the two unit test files, 10 in the connected test.

## Global Constraints

- No migration, no record-format change, no new ADR, no `service.py` change.
- Periodic readiness and the standalone check stay read-only; the append path is unchanged.
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just records`; pre-push
  `just ci`. Commit before controlled-fault arms.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/providers/external_boot_authority/journal.py` | modify | `TornTail`, `load_recovering`, `remove_torn_tail`, shared `_remove_tail`; `_preserve(name, data)` |
| `src/kdive/providers/external_boot_authority/host.py` | modify | torn-tail decision in `_retract_unanchored_tail`, `_is_invalid_lane`, startup trigger, log line |
| `tests/providers/external_boot_authority/test_journal.py` | modify | recovery load and removal tests |
| `tests/providers/external_boot_authority/test_host.py` | modify | decision table, trigger flip |
| `tests/db/test_connected_authority_acceptance.py` | modify | real-startup torn case |

## Task 1 — journal recovery load and removal

Verification:
- Contract: recovery load returns `(complete records, TornTail)` for a torn final line and strict
  `load` still refuses. Mode: focused-test — `test_load_recovering_returns_a_torn_final_line`;
  red: `ImportError: TornTail`; green: `just test-verbose tests/providers/external_boot_authority/test_journal.py`.
- Contract: oversized unterminated, empty, and non-final corrupt lines still raise. Mode:
  focused-test — `test_load_recovering_refuses_every_other_defect`; red: `AttributeError: load_recovering`.
- Contract: removal preserves `retracted/<stem>.torn.<sha256>` (0600) then truncates or unlinks.
  Mode: focused-test — `test_remove_torn_tail_preserves_then_truncates`,
  `test_remove_torn_tail_of_a_torn_only_lane_unlinks`.
- Contract: removal refuses a changed lane or a `TornTail` not from its own latest recovery load;
  append refuses after a torn recovery load. Mode: focused-test —
  `test_remove_torn_tail_refuses_a_changed_lane`, `test_remove_torn_tail_requires_its_recovery_load`,
  `test_append_refuses_after_a_torn_recovery_load`.

Steps:
1. Add to `test_journal.py` (import `hashlib`, `MAX_MESSAGE_BYTES` from `protocol`, `TornTail`):

```python
def _torn_lane(tmp_path: Path) -> tuple[FileAuthorityJournal, JournalRecordV1, bytes]:
    _, first, second = _two_record_lane(tmp_path)
    torn = canonical_record_bytes(second)[:40]
    (tmp_path / "lane.jsonl").write_bytes(canonical_record_bytes(first) + b"\n" + torn)
    return FileAuthorityJournal(tmp_path, "lane.jsonl"), first, torn


def _torn_evidence(tmp_path: Path, torn: bytes) -> Path:
    return tmp_path / RETRACTED_DIRECTORY / f"lane.torn.{hashlib.sha256(torn).hexdigest()}"


def test_load_recovering_returns_a_torn_final_line(tmp_path: Path) -> None:
    journal, first, torn = _torn_lane(tmp_path)
    offset = len(canonical_record_bytes(first)) + 1
    assert journal.load_recovering() == ((first,), TornTail(offset, torn))
    with pytest.raises(ValueError, match="partial final record"):
        journal.load()
    assert FileAuthorityJournal(tmp_path, "other.jsonl").load_recovering() == ((), None)


@pytest.mark.parametrize("defect", ["oversized", "empty", "non-final"])
def test_load_recovering_refuses_every_other_defect(tmp_path: Path, defect: str) -> None:
    _, first, second = _two_record_lane(tmp_path)
    head = canonical_record_bytes(first) + b"\n"
    tail = {
        "oversized": b"x" * (MAX_MESSAGE_BYTES + 1),
        "empty": b"\n" + canonical_record_bytes(second)[:40],
        "non-final": canonical_record_bytes(second)[:40] + b"\n" + b"{",
    }[defect]
    (tmp_path / "lane.jsonl").write_bytes(head + tail)
    with pytest.raises(ValueError):
        FileAuthorityJournal(tmp_path, "lane.jsonl").load_recovering()


def test_remove_torn_tail_preserves_then_truncates(tmp_path: Path) -> None:
    journal, first, torn = _torn_lane(tmp_path)
    _, tail = journal.load_recovering()
    assert tail is not None
    journal.remove_torn_tail(tail)
    assert (tmp_path / "lane.jsonl").read_bytes() == canonical_record_bytes(first) + b"\n"
    evidence = _torn_evidence(tmp_path, torn)
    assert evidence.read_bytes() == torn
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert journal.load() == (first,)


def test_remove_torn_tail_of_a_torn_only_lane_unlinks(tmp_path: Path) -> None:
    torn = canonical_record_bytes(_record())[:40]
    lane = tmp_path / "lane.jsonl"
    lane.write_bytes(torn)
    lane.chmod(0o600)
    journal = FileAuthorityJournal(tmp_path, "lane.jsonl")
    records, tail = journal.load_recovering()
    assert records == () and tail == TornTail(0, torn)
    journal.remove_torn_tail(tail)
    assert not lane.exists()
    assert _torn_evidence(tmp_path, torn).read_bytes() == torn


def test_remove_torn_tail_refuses_a_changed_lane(tmp_path: Path) -> None:
    journal, _, _ = _torn_lane(tmp_path)
    _, tail = journal.load_recovering()
    assert tail is not None
    lane = tmp_path / "lane.jsonl"
    lane.write_bytes(lane.read_bytes() + b"more")
    with pytest.raises(ValueError, match="changed since validation"):
        journal.remove_torn_tail(tail)
    assert lane.read_bytes().endswith(b"more")
    assert not (tmp_path / RETRACTED_DIRECTORY).exists()


def test_remove_torn_tail_requires_its_recovery_load(tmp_path: Path) -> None:
    journal, first, torn = _torn_lane(tmp_path)
    tail = TornTail(len(canonical_record_bytes(first)) + 1, torn)
    with pytest.raises(ValueError, match="recovery load"):
        journal.remove_torn_tail(tail)
    journal.load_recovering()
    with pytest.raises(ValueError, match="recovery load"):
        journal.remove_torn_tail(TornTail(tail.offset, torn[:-1]))
    assert not (tmp_path / RETRACTED_DIRECTORY).exists()


def test_append_refuses_after_a_torn_recovery_load(tmp_path: Path) -> None:
    journal, first, torn = _torn_lane(tmp_path)
    journal.load_recovering()
    before = (tmp_path / "lane.jsonl").read_bytes()
    second = _record(2, record_digest(first), phase=JournalPhase.ADMITTED)
    with pytest.raises(ValueError, match="partial final record"):
        journal.append(second)
    assert (tmp_path / "lane.jsonl").read_bytes() == before
```

2. Run `just test-verbose tests/providers/external_boot_authority/test_journal.py`; expect
   collection error `ImportError: cannot import name 'TornTail'`.
3. In `journal.py`: add `import hashlib`; add

```python
@dataclass(frozen=True, slots=True)
class TornTail:
    """A final line with no newline that an interrupted append left (ADR-0584, #2983)."""

    offset: int
    data: bytes
```

   Initialise `self._torn: tuple[TornTail, _FileIdentity] | None = None` in `__init__`. Rename
   the body of `load` to `load_recovering(self, *, deadline: float | None = None) ->
   tuple[tuple[JournalRecordV1, ...], TornTail | None]`, which sets `self._torn = None` first and
   returns `((), None)` for an absent lane. Replace the partial-line check in its read loop with

```python
                    if not line.endswith(b"\n"):
                        if len(line) > MAX_MESSAGE_BYTES:
                            raise ValueError("authority journal record is empty or oversized")
                        if consumed != size:
                            raise ValueError("authority journal changed during validation")
                        torn = TornTail(consumed - len(line), line)
                        break
```

   (with `torn: TornTail | None = None` before the loop). After the loop, when `torn` is not
   None: `self._cache = None`, `self._torn = (torn, self._identity(status))`, return
   `(tuple(records), torn)`; otherwise build the cache as today and return `(result, None)`.
   `load` becomes:

```python
    def load(self, *, deadline: float | None = None) -> tuple[JournalRecordV1, ...]:
        """Load and verify exact canonical bytes, sequence, chain, lane, and ownership."""
        records, torn = self.load_recovering(deadline=deadline)
        if torn is not None:
            raise ValueError("authority journal has a partial final record")
        return records
```

4. Move `retract`'s open/verify/preserve/truncate/unlink/fsync/reload body into
   `_remove_tail(self, offset: int, data: bytes, identity: _FileIdentity | None, evidence: str)
   -> None`, comparing `self._identity(status) != identity` and `pread(len(data), offset) !=
   data`. `retract` keeps its final-record check and calls it with `cache.tail_offset`, `encoded`,
   `cache.identity`, and `f"{record.system_id}.{record.sequence}.{digest}.jsonl"`.
   `_preserve(self, name: str, data: bytes)` takes the name; its comment says the name carries a
   digest of the bytes. Add:

```python
    def remove_torn_tail(self, torn: TornTail) -> None:
        """Remove a torn final line after preserving its bytes (ADR-0584 amendment, #2983)."""
        observed, self._torn = self._torn, None
        if observed is None or observed[0] != torn:
            raise ValueError("authority journal torn-tail removal requires its recovery load")
        digest = hashlib.sha256(torn.data).hexdigest()
        stem = self._name.removesuffix(".jsonl")
        self._remove_tail(torn.offset, torn.data, observed[1], f"{stem}.torn.{digest}")
```

5. Run the step 2 command; expect all pass (existing retract tests included). Commit
   `feat(authority): recover a torn journal tail in a recovery load`.

## Task 2 — host decision and startup trigger

Verification:
- Contract: `_retract_unanchored_tail` recovers a torn tail only after the head's own record or
  in a headless empty lane, and refuses every other torn shape with `journal: invalid-lane`
  without touching the lane. Mode: focused-test — `test_startup_recovers_a_torn_unanchored_tail`,
  `test_startup_refuses_every_other_torn_tail`; red: `journal: invalid-lane` on the recover cases.
- Contract: startup reconciles after `journal: invalid-lane`. Mode: focused-test —
  `test_startup_reconciles_only_after_a_journal_head_difference` with the `invalid-lane` row
  flipped to `True`; red: `assert [] == [config]`.

Steps:
1. In `test_host.py` import `TornTail` from `...journal`, flip that parametrize row to
   `(HostReadinessError("journal", "invalid-lane"), True)`, and add:

```python
def _write_torn_lane(
    config: AuthorityHostConfig, complete: list[JournalRecordV1], torn: JournalRecordV1
) -> tuple[Path, bytes]:
    lane = config.journal_dir / f"{torn.system_id}.jsonl"
    partial = canonical_record_bytes(torn)[:40]
    lane.write_bytes(b"".join(canonical_record_bytes(r) + b"\n" for r in complete) + partial)
    lane.chmod(0o600)
    return lane, partial


@pytest.mark.parametrize("complete", [2, 0])
def test_startup_recovers_a_torn_unanchored_tail(tmp_path: Path, complete: int) -> None:
    config = _config(tmp_path)
    records = _chain(config, uuid4(), 3)
    lane, partial = _write_torn_lane(config, records[:complete], records[complete])
    head = _head_of(records[complete - 1]) if complete else None
    system_id = str(records[0].system_id)

    probe = host._retract_unanchored_tail(config, system_id, head, retract=False)  # noqa: SLF001
    assert isinstance(probe, TornTail) and lane.read_bytes().endswith(partial)
    tail = host._retract_unanchored_tail(config, system_id, head, retract=True)  # noqa: SLF001

    assert tail == probe
    assert next((config.journal_dir / "retracted").iterdir()).read_bytes() == partial
    if head is not None:
        restore_journal_inventory(config, (head,))
    else:
        assert not lane.exists()
        restore_journal_inventory(config, ())


@pytest.mark.parametrize(
    ("complete", "anchored", "moved"),
    [(2, 1, False), (1, 2, False), (1, 0, False), (0, 1, False), (2, 2, True)],
    ids=["after-unanchored-record", "head-ahead", "headless-record", "head-no-record", "moved"],
)
def test_startup_refuses_every_other_torn_tail(
    tmp_path: Path, complete: int, anchored: int, moved: bool
) -> None:
    config = _config(tmp_path)
    records = _chain(config, uuid4(), 3)
    lane, _ = _write_torn_lane(config, records[:complete], records[complete])
    head = _head_of(records[anchored - 1]) if anchored else None
    if moved and head is not None:
        head = replace(head, digest="sha256:" + "9" * 64)
    before = lane.read_bytes()

    with pytest.raises(HostReadinessError, match="journal: invalid-lane"):
        host._retract_unanchored_tail(  # noqa: SLF001
            config, str(records[0].system_id), head, retract=True
        )

    assert lane.read_bytes() == before
    assert not (config.journal_dir / "retracted").exists()
```

2. Run `just test-verbose tests/providers/external_boot_authority/test_host.py`; expect the
   recover cases and the flipped trigger row to fail.
3. In `host.py` import `TornTail` and `hashlib`; add

```python
def _is_invalid_lane(error: HostReadinessError) -> bool:
    return error.component == "journal" and error.reason == "invalid-lane"


def _torn_tail_is_unanchored(
    config: AuthorityHostConfig, records: tuple[JournalRecordV1, ...], head: JournalHead | None
) -> bool:
    """A torn line follows the head's own record, or is all a headless lane holds (#2983)."""
    if head is None:
        return not records
    return bool(records) and _head_matches(records[-1], head, config)
```

   use `_is_invalid_lane(error)` in `_may_be_in_flight_anchor`; change `_retract_unanchored_tail`
   to return `JournalRecordV1 | TornTail | None` with the body

```python
        records, torn = journal.load_recovering(deadline=deadline)
        if torn is not None:
            if not _torn_tail_is_unanchored(config, records, head):
                raise HostReadinessError("journal", "invalid-lane")
            if retract:
                journal.remove_torn_tail(torn)
            return torn
        tail = _unanchored_tail(config, system_id, records, head)
```

   In `_reconcile_journal_tails` log a `TornTail` as `"authority journal removed a torn final
   line at startup"` with `system_id`, `offset`, `length`, `sha256` (hex of the data), keeping
   the existing record log line for a `JournalRecordV1`. In `run_authority_host` reconcile when
   `_is_head_divergence(error) or _is_invalid_lane(error)`.
4. Run the step 2 command; expect all pass. Commit
   `feat(authority): recover a torn unanchored tail at startup`.

## Task 3 — real startup proof

Verification:
- Contract: the real startup with Postgres heads recovers a refused record torn mid-append.
  Mode: focused-test — `test_host_startup_retracts_a_refused_anchor_left_by_a_failed_retraction[torn]`;
  red on Task 1–2 reverted: startup raises `journal: invalid-lane`.

Steps:
1. Parametrize that test with `@pytest.mark.parametrize("torn", [False, True], ids=["record",
   "torn"])`. After `monkeypatch.undo()`, when `torn`, rewrite `lane.write_bytes(unanchored[:-10])`
   and expect the evidence to equal `unanchored[len(anchored) : -10]`.
2. Run `just test-verbose tests/db/test_connected_authority_acceptance.py`; expect both pass.
   Commit `test(authority): prove startup recovers a torn tail end to end`.

## Controlled faults (after the last commit)

Each arm edits one line, runs the named focused file, expects red, then `git checkout -- <file>`:
`_torn_tail_is_unanchored` returns `True` (refusal table red); drop the `_preserve` call in
`_remove_tail` (evidence asserts red); drop the identity comparison (changed-lane test red);
drop the `len(line) > MAX_MESSAGE_BYTES` check (oversized case red); revert the trigger
(flipped row red).
