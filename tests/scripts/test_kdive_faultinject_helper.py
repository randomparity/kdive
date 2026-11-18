"""Contract checks for the in-guest kdive-faultinject helper (ADR-0822).

The helper writes debugfs as root, so these run it against a fake debugfs tree and assert the
closed knob/parameter allowlist, the bounds, the write order and the distinct exit codes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path("deploy/remote-libvirt-guest-helpers/kdive-faultinject")

PARAMS = {
    "failslab": ("probability", "interval", "times", "space", "verbose"),
    "fail_page_alloc": ("probability", "interval", "times", "space", "verbose", "min-order"),
}
DEFAULTS = {
    "probability": 0,
    "interval": 1,
    "times": 1,
    "space": 0,
    "verbose": 2,
    "min-order": 0,
}
BOUNDS = {
    "probability": (0, 100),
    "interval": (1, 1000000),
    "times": (-1, 1000000),
    "space": (0, 1073741824),
    "verbose": (0, 2),
    "min-order": (0, 10),
}


@pytest.fixture
def debugfs(tmp_path: Path) -> Path:
    root = tmp_path / "debug"
    (root / "tracing").mkdir(parents=True)
    for knob, params in PARAMS.items():
        (root / knob).mkdir()
        for param in params:
            (root / knob / param).write_text("7\n")
    return root


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "FAULTINJECT_DEBUGFS_ROOT": str(root)}
    return subprocess.run(
        ["bash", str(HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_read_all_reports_every_knob(debugfs: Path) -> None:
    proc = run(debugfs, "read")
    assert proc.returncode == 0, proc.stderr
    knobs = json.loads(proc.stdout)["knobs"]
    assert set(knobs) == set(PARAMS)
    assert knobs["failslab"] == {"present": True, **dict.fromkeys(PARAMS["failslab"], 7)}
    assert knobs["fail_page_alloc"]["min-order"] == 7
    assert "min-order" not in knobs["failslab"]


def test_read_one_knob(debugfs: Path) -> None:
    proc = run(debugfs, "read", "failslab")
    assert proc.returncode == 0, proc.stderr
    assert set(json.loads(proc.stdout)["knobs"]) == {"failslab"}


def test_set_writes_pairs_and_prints_the_new_state(debugfs: Path) -> None:
    proc = run(debugfs, "set", "fail_page_alloc", "times=-1", "min-order=3", "probability=25")
    assert proc.returncode == 0, proc.stderr
    state = json.loads(proc.stdout)["knobs"]["fail_page_alloc"]
    assert (state["times"], state["min-order"], state["probability"]) == (-1, 3, 25)
    assert (debugfs / "failslab" / "probability").read_text() == "7\n"


@pytest.mark.parametrize("knob", sorted(PARAMS))
def test_reset_restores_the_defaults_of_the_exposed_parameters(debugfs: Path, knob: str) -> None:
    proc = run(debugfs, "reset", knob)
    assert proc.returncode == 0, proc.stderr
    state = json.loads(proc.stdout)["knobs"][knob]
    assert {k: v for k, v in state.items() if k != "present"} == {
        p: DEFAULTS[p] for p in PARAMS[knob]
    }
    other = next(k for k in PARAMS if k != knob)
    assert (debugfs / other / "probability").read_text() == "7\n"


def test_reset_with_no_knob_resets_every_present_knob(debugfs: Path) -> None:
    (debugfs / "failslab" / "probability").write_text("50\n")
    for path in (debugfs / "fail_page_alloc").iterdir():
        path.unlink()
    (debugfs / "fail_page_alloc").rmdir()
    proc = run(debugfs, "reset")
    assert proc.returncode == 0, proc.stderr
    knobs = json.loads(proc.stdout)["knobs"]
    assert knobs["failslab"]["probability"] == 0
    assert knobs["fail_page_alloc"] == {"present": False}


@pytest.mark.parametrize("param", sorted(BOUNDS))
def test_every_bound_is_accepted_at_its_edge_and_refused_beyond(debugfs: Path, param: str) -> None:
    knob = "fail_page_alloc"
    low, high = BOUNDS[param]
    for value in (low, high):
        proc = run(debugfs, "set", knob, f"{param}={value}")
        assert proc.returncode == 0, (param, value, proc.stderr)
        assert (debugfs / knob / param).read_text() == f"{value}\n"
    before = snapshot(debugfs)
    for value in (low - 1, high + 1):
        proc = run(debugfs, "set", knob, f"{param}={value}")
        assert proc.returncode == 2, (param, value)
        assert json.loads(proc.stderr)["error"] in {"out_of_range", "bad_value"}
        assert snapshot(debugfs) == before


@pytest.mark.parametrize(
    "args",
    [
        ("set", "slab_alloc", "probability=1"),
        ("set", "../failslab", "probability=1"),
        ("set", "failslab", "min-order=1"),
        ("set", "failslab", "task-filter=1"),
        ("set", "failslab", "../x=1"),
        ("set", "failslab", "a/b=1"),
        ("set", "failslab", "probability=1;id"),
        ("set", "failslab", "probability=1 2"),
        ("set", "failslab", "probability=$(id)"),
        ("set", "failslab", "probability=../../x"),
        ("set", "failslab", "probability="),
        ("set", "failslab", "=5"),
        ("set", "failslab", "probability"),
        ("set", "failslab", "probability=+5"),
        ("set", "failslab", "probability=--5"),
        ("set", "failslab", "probability=-0"),
        ("set", "failslab", "probability=007"),
        ("set", "failslab", "probability=-1"),
        ("set", "failslab", "space=12345678901"),
        ("set", "failslab", "probability=1", "probability=2"),
        ("set", "failslab"),
        ("set", "failslab", "interval=5", "probability=999"),
        ("set", "failslab", "interval=5", "bogus=1"),
        ("read", "slab_alloc"),
        ("read", "failslab", "extra"),
        ("reset", "../failslab"),
        ("reset", "failslab", "extra"),
        ("frobnicate",),
        (),
    ],
)
def test_rejected_input_exits_2_and_writes_nothing(debugfs: Path, args: tuple[str, ...]) -> None:
    before = snapshot(debugfs)
    proc = run(debugfs, *args)
    assert proc.returncode == 2, proc.stdout
    assert proc.stdout == ""
    assert "error" in json.loads(proc.stderr)
    assert snapshot(debugfs) == before
    assert not (debugfs.parent / "x").exists()


def _write_order(root: Path, *args: str) -> list[str]:
    """Parameters in the order the helper wrote them, read from its own xtrace."""
    env = {**os.environ, "FAULTINJECT_DEBUGFS_ROOT": str(root)}
    proc = subprocess.run(
        ["bash", "-x", str(HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [
        line.split()[3] for line in proc.stderr.splitlines() if line.startswith("+ write_param ")
    ]


def test_set_writes_probability_last(debugfs: Path) -> None:
    order = _write_order(
        debugfs, "set", "fail_page_alloc", "probability=5", "interval=2", "times=3"
    )
    assert order == ["interval", "times", "probability"]


def test_reset_writes_probability_first(debugfs: Path) -> None:
    order = _write_order(debugfs, "reset", "fail_page_alloc")
    assert order[0] == "probability"
    assert sorted(order) == sorted(PARAMS["fail_page_alloc"])


def test_missing_or_empty_debugfs_exits_3(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    for root in (tmp_path / "absent", empty):
        for args in (
            ("read",),
            ("read", "failslab"),
            ("set", "failslab", "probability=1"),
            ("reset",),
        ):
            proc = run(root, *args)
            assert proc.returncode == 3, (root, args)
            assert json.loads(proc.stderr)["error"] == "debugfs_not_mounted"


def test_missing_knob_exits_4_but_read_all_reports_it_absent(debugfs: Path) -> None:
    for path in (debugfs / "failslab").iterdir():
        path.unlink()
    (debugfs / "failslab").rmdir()
    for args in (("read", "failslab"), ("set", "failslab", "probability=1"), ("reset", "failslab")):
        proc = run(debugfs, *args)
        assert proc.returncode == 4, args
        assert json.loads(proc.stderr)["error"] == "knob_absent"
    proc = run(debugfs, "read")
    assert proc.returncode == 0
    knobs = json.loads(proc.stdout)["knobs"]
    assert knobs["failslab"] == {"present": False}
    assert knobs["fail_page_alloc"]["present"] is True


@pytest.mark.parametrize("content", ["not-a-number\n", "0x10\n", ""])
def test_unparseable_parameter_file_exits_5(debugfs: Path, content: str) -> None:
    (debugfs / "failslab" / "interval").write_text(content)
    proc = run(debugfs, "read", "failslab")
    assert proc.returncode == 5
    assert json.loads(proc.stderr)["error"] == "unparseable_value"


def test_missing_parameter_file_exits_5_and_is_not_created(debugfs: Path) -> None:
    (debugfs / "failslab" / "space").unlink()
    proc = run(debugfs, "set", "failslab", "space=4")
    assert proc.returncode == 5
    assert not (debugfs / "failslab" / "space").exists()
    assert run(debugfs, "read", "failslab").returncode == 5
