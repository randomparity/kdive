# Issue 890 Build-Id Note Design

## Problem

External `vmlinux` validation rejects ELF files whose GNU build-id note is visible to
`readelf -n` but lives in an SHT_NOTE section with a name other than
`.note.gnu.build-id`. The parser already identifies GNU build-id records by note owner and
type; the current section-name filter is stricter than the user-facing contract.

## Contract

`runs.complete_build` must accept a GNU build-id note in any bounded SHT_NOTE section.
The section name is not part of the external upload contract. Validation still rejects
ELFs with no GNU build-id note and keeps the existing bounds checks for section headers,
section size, and object size.

If an SHT_NOTE section is structurally valid but does not contain a GNU build-id record,
validation should continue scanning later note sections. Malformed note contents can still
surface as the existing build-failure category.

## Implementation

Change `_find_build_id_note()` to read every SHT_NOTE section and call
`parse_gnu_build_id()`. If a section has no GNU build-id note, continue to the next
SHT_NOTE section. If no note section contains a build-id, raise a build failure saying the
ELF carries no GNU build-id note.

## Testing

Add a unit fixture/test in `tests/providers/local_libvirt/test_validate_external_artifacts.py`
for a minimal ELF whose GNU build-id note section is named `.notes`. The test should prove
the ranged extractor returns the same build-id from that nonstandard note section.
