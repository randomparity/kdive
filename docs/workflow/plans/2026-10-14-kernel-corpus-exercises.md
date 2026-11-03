# Plan: deterministic broken/fixed kernel corpus (#2817)

Spec: `docs/workflow/specs/2026-10-14-kernel-corpus-exercises-design.md`.
Branch `feat/kernel-corpus-2817` from `origin/main` `781c6b01d`.

## Tasks

1. **Corpus selections.** Add `fixtures/kernel/corpus.toml` (four full commits, torvalds
   repository). `scripts/kernel_fixtures.py` gains `selections()`; `baseline_selection` and the
   CLI `--baseline` choices read both TOML files.
   Verify: `tests/scripts/test_kernel_fixtures.py` covers the selection set, an unknown name, the
   CLI accepting a corpus name, and a corpus selection that records and verifies under its own
   name only. A real build of each revision verifies with `kernel_fixtures.py verify`.
2. **Case module.** `tests/integration/live_stack/kernel_corpus.py`: the case table, cell
   helpers, `same_config`, `symbol_sizes`/`oops_frames`/`resolve_frames`/`crash_evidence`,
   `clean_console`, `pci_inventory`/`nlink_outcome`, `corpus_body`, `bindings` and `main`.
   Verify: `tests/integration/live_stack/test_kernel_corpus.py` (selections and notes, config
   equality, symbol sizes, oops resolution and its rejections, clean console, nlink verdicts,
   bindings per revision, install `cmdline`, expected-crash read). The oops check is also run
   against the pre-check console and `System.map`.
3. **Carrier and binding.** `tests/integration/test_kernel_corpus_live.py::test_kernel_corpus`
   on `on_catalog_system`; four `[implementations]` rows; `test_coverage_contract.py` counts the
   corpus cells as bound.
   Verify: the live module collects four x86_64 parameters; the contract test passes.
4. **Runbook.** Corpus section in `docs/operating/runbooks/live-testing.md`, later the last-run
   record.
5. **Live proof.** On an assigned local Fedora 44 KVM guest: build the four fixtures, stage the
   image, bring up `demo-up.sh` at the committed candidate, bindings, run, assemble, qualify, tear
   down. Record outcomes honestly.

## Guardrails

`just lint`, `just type`, focused `just test-verbose` paths, then `just ci` before push.
Each new check is shown to bite with a committed-first controlled fault.
