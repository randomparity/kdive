# Readiness marker after terminal escape sequences — plan (#2907)

**Goal:** a `kdive-ready` marker preceded on its line by another console writer's escape
sequences classifies `ready`, and future images write the marker on its own line.

**Architecture:** `_scan_console` in the local-libvirt readiness module strips complete 7-bit
ECMA-48 escape sequences before its marker and crash-signature scans; `readiness_unit` renders a
single `printf` that starts a fresh line. Spec:
[`2026-09-29-readiness-marker-escape-sequences-design.md`](../specs/2026-09-29-readiness-marker-escape-sequences-design.md).

**Tech stack:** Python 3.14, `re`, pytest, `uv`, `just`.

Expected implementation size: 60–100 changed lines (M) — two small source edits plus the tests in
the two task file maps below.

## Global Constraints

- Python 3.14 managed by `uv`; no new dependency.
- `just lint` and `just type` stay green; lines ≤ 100 characters (ruff).
- The marker constant stays `kdive-ready`; the marker regex in `_scan_console` is unchanged.
- Commit messages follow Conventional Commits.

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` | modify | console verdict (`_scan_console`, `classify_console`) |
| `tests/providers/local_libvirt/test_install.py` | modify | `classify_console` unit tests |
| `src/kdive/images/families/_fedora_customize.py` | modify | `readiness_unit` rendering |
| `tests/images/families/test_fedora_customize.py` | modify | `readiness_unit` tests |

No caller migrates: `classify_console(data, *, marker=...)` and
`readiness_unit(kdump_unit, console_device)` keep their signatures.

## Task 1 — host matcher strips escape sequences

**Interfaces:** consumes `first_crash_signature(text: str) -> re.Match[str] | None` from
`kdive.domain.lifecycle.crash_signatures` (existing). Provides unchanged
`classify_console(data: bytes, *, marker: str = "kdive-ready") -> ConsoleVerdict`.

**Verification:**
- Contract: captured getty escape bytes before the marker classify `ready`. Mode: focused-test —
  `test_classify_marker_after_getty_escape_sequences_is_ready`; red: `AssertionError` with
  `'pending' == 'ready'`; green:
  `just test-verbose tests/providers/local_libvirt/test_install.py -k classify`.
- Contract: token shape kept through stripping. Mode: focused-test —
  `test_classify_escape_sequences_keep_the_marker_token_shape`; a guard that passes before and
  after the change; red observed by the controlled fault of substituting `" "` instead of `""`
  for each sequence, which turns `foo\x1b[0mkdive-ready` into `ready`; green with the command
  above.
- Contract: crash before an escaped marker still wins. Mode: focused-test —
  `test_classify_crash_before_escaped_marker_wins`; green with the same command.

Steps:

1. Add to `tests/providers/local_libvirt/test_install.py`, after
   `test_classify_marker_glued_to_prefix_token_is_pending`:

   ```python
   # #2907: the Fedora 44 serial getty wrote an OSC 3008 context record and a DCS XTGETTCAP query,
   # each ST-terminated, onto the marker's line immediately before the readiness unit's echo.
   _GETTY_ESCAPES_THEN_MARKER = (
       b"\x1b]3008;start=4f1c;user=root;hostname=localhost;pid=812;"
       b"unit=serial-getty@ttyS0.service;type=service\x1b\\"
       b"\x1bP+q6E616D65\x1b\\"
       b"kdive-ready\r\n"
   )


   def test_classify_marker_after_getty_escape_sequences_is_ready() -> None:
       data = b"[   14.90] systemd[1]: Started serial-getty@ttyS0.service.\r\n"
       assert classify_console(data + _GETTY_ESCAPES_THEN_MARKER, marker=_MARKER) == "ready"


   @pytest.mark.parametrize(
       "line",
       [
           b"[  OK  ] Started \x1b[0;1;39mkdive-ready.service\x1b[0m - Signal readiness.\r\n",
           b"fookdive-ready\x1b[0m\r\n",
           b"foo\x1b[0mkdive-ready\r\n",
           b"\x1b]3008;unit=kdive-ready\x1b\\\r\n",
       ],
   )
   def test_classify_escape_sequences_keep_the_marker_token_shape(line: bytes) -> None:
       assert classify_console(line, marker=_MARKER) == "pending"


   def test_classify_crash_before_escaped_marker_wins() -> None:
       data = b"[    1.0] Kernel panic - not syncing\r\n" + _GETTY_ESCAPES_THEN_MARKER
       assert classify_console(data, marker=_MARKER) == "crashed"
   ```

2. Run `just test-verbose tests/providers/local_libvirt/test_install.py -k classify`. Expect
   exactly one failure: `test_classify_marker_after_getty_escape_sequences_is_ready`
   (`'pending' == 'ready'`).
3. In `readiness.py`, after `_MAX_CONSOLE_WINDOW_BYTES`, add:

   ```python
   # Complete 7-bit ECMA-48 escape sequences, removed before the marker and crash scans (#2907):
   # control strings (DCS/OSC/SOS/PM/APC) ended by ST or BEL on their own line, CSI, and other ESC
   # sequences. An unterminated sequence stays, so the removal never spans a line.
   _ESCAPE_SEQUENCE = re.compile(
       r"\x1b[P\]X^_][^\x07\x1b\n]*(?:\x07|\x1b\\)"
       r"|\x1b\[[0-?]*[ -/]*[@-~]"
       r"|\x1b(?![\[\]PX^_])[ -/]*[0-~]"
   )
   ```

   and change the first line of `_scan_console` to:

   ```python
   text = _ESCAPE_SEQUENCE.sub("", data.decode("utf-8", errors="replace"))
   ```

4. Rerun the step 2 command. Expect all `classify` tests to pass.
5. Controlled faults, one at a time: restore the old first line of `_scan_console` (the step 2
   failure returns); substitute `" "` for `""` in `_ESCAPE_SEQUENCE.sub` (the token-shape guard
   fails on `foo\x1b[0mkdive-ready`).
   Revert with `git checkout -- src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py`
   only after committing step 3.
6. `just lint && just type`, then commit `fix(local-libvirt): match kdive-ready after console
   escape sequences`.

## Task 2 — readiness unit writes the marker on its own line

**Interfaces:** provides unchanged `readiness_unit(kdump_unit: str, console_device: str) -> str`
from `kdive.images.families._fedora_customize`, consumed by `rootfs_build.py` and the rhel,
debian, and suse families through `ctx.readiness_unit_path`.

**Verification:**
- Contract: the rendered `ExecStart` is one `printf` of `\nkdive-ready\n` to the arch console.
  Mode: focused-test — `test_readiness_unit_writes_the_marker_on_its_own_line[ttyS0|hvc0]`; red:
  `AssertionError` (the `echo` line is rendered); green:
  `just test-verbose tests/images/families/test_fedora_customize.py`.

Steps:

1. Add to `tests/images/families/test_fedora_customize.py`, after
   `test_readiness_unit_targets_the_arch_console_device`:

   ```python
   @pytest.mark.parametrize("console_device", ["ttyS0", "hvc0"])
   def test_readiness_unit_writes_the_marker_on_its_own_line(console_device: str) -> None:
       # #2907: one printf that starts a fresh line, so bytes another console writer (the serial
       # getty) left on the current line cannot glue to the marker.
       unit = readiness_unit("kdump.service", console_device)
       expected = f"ExecStart=/bin/sh -c 'printf \"\\nkdive-ready\\n\" > /dev/{console_device}'"
       assert expected in unit.splitlines()
   ```

2. Run `just test-verbose tests/images/families/test_fedora_customize.py`. Expect both new
   parameters to fail.
3. In `readiness_unit`, replace the `ExecStart` line of the returned f-string with:

   ```python
   ExecStart=/bin/sh -c 'printf "\\n{READINESS_MARKER}\\n" > /dev/{console_device}'
   ```

   and add to the docstring, after the `network-online.target` paragraph: "The marker is written
   by one ``printf`` that starts with a newline (#2907): the serial getty writes terminal escape
   sequences to the same device, and the marker must not share their line."
4. Rerun the step 2 command. Expect all tests to pass.
5. Controlled fault: after committing, restore the `echo` line; the new tests fail. Revert with
   `git checkout -- src/kdive/images/families/_fedora_customize.py`.
6. `just lint && just type`, then commit `fix(images): write kdive-ready on its own console line`.

## Live proof (after both tasks)

On the Fedora 44 x86_64 lab host, under the campaign host lock: deploy the branch, confirm the
deployed code contains `_ESCAPE_SEQUENCE`, rebuild a rhel-family image, run the stack through
`scripts/demo-up.sh`, run `test_family_guest_is_ssh_reachable_over_the_wire[rhel]`, and read the
console log bytes around the marker to confirm it starts its own line.
