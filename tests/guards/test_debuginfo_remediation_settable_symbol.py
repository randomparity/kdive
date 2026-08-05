"""Guard: debuginfo remediation strings name a settable symbol, not the bare bool (#1871).

``CONFIG_DEBUG_INFO`` (``lib/Kconfig.debug:249``) is a prompt-less bool that no config fragment
can set and that ``make olddefconfig`` forces off — ``src/kdive/kernel_config/requirements.py``
records this in its own words, which is why ``FEATURE_REQUIREMENTS`` advertises the DWARF choice
members (``DEBUG_INFO_DWARF5`` et al.) instead of ``DEBUG_INFO`` itself. A remediation string that
tells an agent or operator to set ``CONFIG_DEBUG_INFO=y`` sends them to add a line that vanishes
on the next ``olddefconfig`` run and hit the identical error again.

#1853 / PR #1874 fixed this class in the two docs surfaces and added
``tests/mcp/resources/test_kernel_config_contract_docs.py`` to guard them, but that guard scans
markdown only. #1871 is the same defect in runtime remediation strings, which that guard cannot
see. This guard closes that gap for the two source files known to carry the pattern.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).parents[2]

# Every file known to build a debuginfo remediation string for a human or agent to act on.
_FILES = (
    _ROOT / "src/kdive/providers/shared/debug_common/gdbmi/policy/debuginfo.py",
    _ROOT / "scripts/live-debug.py",
)

# The bare, prompt-less bool. A trailing `=y` (or word boundary) distinguishes it from the
# prompted DWARF5/DWARF4/BTF/... symbols, all of which start with this same prefix.
_BARE_SYMBOL = re.compile(r"CONFIG_DEBUG_INFO(?!_)\b")

_PROMPTED_SYMBOL = "CONFIG_DEBUG_INFO_DWARF5"


def test_debuginfo_remediation_never_names_the_bare_prompt_less_bool() -> None:
    for path in _FILES:
        text = path.read_text()
        offenders = _BARE_SYMBOL.findall(text)
        assert not offenders, f"{path} tells an agent to set the unsettable CONFIG_DEBUG_INFO"


def test_debuginfo_remediation_names_a_prompted_symbol() -> None:
    # Vacuity check: the guard above passes trivially if the remediation text disappears
    # entirely. Assert the settable symbol is still there to act on.
    for path in _FILES:
        text = path.read_text()
        assert _PROMPTED_SYMBOL in text, f"{path} lost its debuginfo remediation entirely"
