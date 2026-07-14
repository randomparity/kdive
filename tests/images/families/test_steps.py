"""Unit tests for the typed customization :mod:`Step` value objects (ADR-0345, #1147)."""

from __future__ import annotations

from pathlib import Path

from kdive.images.families.steps import (
    InstallPackages,
    Mkdir,
    RunCommand,
    StageFile,
    UploadFile,
    WriteFile,
)


def test_steps_are_frozen_value_objects():
    assert Mkdir("/d").path == "/d"
    assert WriteFile("/f", "x").content == "x"
    assert StageFile("/f", "y").content == "y"
    assert UploadFile(Path("/h"), "/g", mode="0755").mode == "0755"
    assert InstallPackages(("a", "b")).names == ("a", "b")
    assert RunCommand("echo hi").sh == "echo hi"


def test_uploadfile_mode_defaults_none():
    assert UploadFile(Path("/h"), "/g").mode is None
