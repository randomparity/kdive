# Readiness marker after terminal escape sequences — design (#2907)

- **Issue:** [#2907](https://github.com/randomparity/kdive/issues/2907)
- **Governing decision:** [ADR-0055](../../adr/0055-install-readiness-kdump-seam.md) §3, amended
  by this change (2026-09-29 amendment).

## Problem

Local-libvirt first boot waits for the guest's `kdive-ready` line on the serial console. The
readiness unit echoes the marker to `/dev/<console>` while `serial-getty@<console>` writes to
the same device. On a Fedora 44 guest the getty wrote a systemd OSC 3008 context record and a
DCS XTGETTCAP query, both terminated by ST (`ESC \`), immediately before the marker on the same
line:

```
…;type=service ESC \ ESC P+q6E616D65 ESC \ kdive-ready CR LF
```

`_scan_console` requires the marker to start a line or follow horizontal whitespace, so the
preceding `\` of the ST keeps the verdict `pending` for the whole boot window and provisioning
fails with `provisioning_failure` (`failure_detail_first_boot: timeout`).

## Design

Two independent layers, each sufficient for the observed capture.

1. **Host matcher (covers already-built images).** `_scan_console` removes complete ECMA-48
   escape sequences from the decoded console text before it searches for the marker and the
   crash signatures. The marker pattern itself is unchanged. The recognised forms, all 7-bit:
   - control strings: `ESC` + one of `P ] X ^ _` (DCS, OSC, SOS, PM, APC), a body with no
     `ESC`, `BEL`, or `LF`, then `ST` (`ESC \`) or `BEL`;
   - CSI: `ESC [`, parameter bytes `0x30–0x3F`, intermediate bytes `0x20–0x2F`, final byte
     `0x40–0x7E`;
   - other escapes: `ESC`, intermediate bytes `0x20–0x2F`, final byte `0x30–0x7E`, excluding the
     control-string and CSI introducers above.

   An incomplete sequence (no terminator on its line) is left in place, so the stripping never
   spans a line and never removes text that a terminal would print. The pre-marker crash region
   becomes a prefix of the stripped text, so crash-before-marker still wins.
2. **Guest unit (covers future images).** `readiness_unit` renders
   `ExecStart=/bin/sh -c 'printf "\nkdive-ready\n" > /dev/<console>'`. Whether systemd's
   C-escape processing turns `\n` into a newline before the shell runs or leaves it for `printf`,
   the output is the same one write of `\nkdive-ready\n`, which starts a fresh line. Whatever
   another writer left on the current line stays on that line.

The two layers are coupled only by the regression test that feeds the captured byte shape
through `classify_console`.

## Success

- `classify_console` returns `ready` for the captured byte shape (criterion 1).
- `kdive-ready.service`, `fookdive-ready`, and a marker glued behind a CSI to a preceding token
  (`foo\x1b[0mkdive-ready`) stay `pending` (criterion 2).
- A crash signature before the marker still yields `crashed`, with or without escape
  sequences on the marker line (criterion 3).
- The rendered unit writes `\nkdive-ready\n` to the arch console device (criterion 4).
- Each layer's test fails when that layer is reverted (criterion 5).
- `test_family_guest_is_ssh_reachable_over_the_wire[rhel]` passes on a Fedora 44 x86_64 lab
  host with a rhel-family image rebuilt from the branch, and the captured console shows the
  marker on its own line (criterion 6).

## Failure model

1. **Actors and deployments**
   - the local-libvirt provisioning worker reading its own domain's console log;
   - the composition-root readiness fallback (`system_authority/composition.py`) reading the same
     log;
   - images built by the rhel, debian, and suse families from this branch onward.
2. **Invariants and assets at stake**
   - no `ready` verdict without a guest-written `kdive-ready` token on its own line or after
     horizontal whitespace;
   - a crash signature printed before the marker yields `crashed` (ADR-0055 §3).
3. **Accepted failure classes**
   - guest-controlled output can print the marker itself; unchanged from today and outside the
     anti-spoof goal, which only excludes incidental substrings such as unit names;
   - text inside a complete same-line control string is dropped before the crash scan; a
     terminal does not print control-string bodies, so a kernel crash line cannot be one;
   - 8-bit C1 controls are not recognised: `errors="replace"` already turns a lone `0x9B` into
     U+FFFD, and no observed writer emits them;
   - the single-write property of `printf` depends on the image's `/bin/sh`; if a shell split the
     write, layer 1 still classifies the line.
   - one live boot does not prove the race closed; the regression test from the captured bytes
     is the durable proof.
4. **Covered elsewhere**
   - the remote-libvirt console collector's crash scan — separate plane, excluded by the
     operator;
   - getty and other console writers' configuration — not owned, excluded by the operator;
   - rebuilding published catalog images — operator-owned.

## Validation

- `tests/providers/local_libvirt/test_install.py`: captured-bytes regression (`ready`); CSI-glued
  and escaped unit-name negatives (`pending`); crash-before-escaped-marker (`crashed`).
- `tests/images/families/test_fedora_customize.py`: the unit's `ExecStart` writes
  `\nkdive-ready\n` to `/dev/ttyS0` and `/dev/hvc0`.
- Controlled faults: revert each layer on its own and observe its test go red.
- Live: the rhel-family SSH-reachability proof on a Fedora 44 x86_64 lab host through
  `scripts/demo-up.sh`.
