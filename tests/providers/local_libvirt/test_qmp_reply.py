"""The local QMP passthrough reply parser (ADR-0385 amendment, #3270)."""

from __future__ import annotations

import json

import pytest

from kdive.providers.local_libvirt.lifecycle.qmp_reply import (
    MalformedQmpReplyError,
    parse_qmp_reply,
)


def test_a_return_reply_carries_its_value_and_id() -> None:
    reply = parse_qmp_reply(json.dumps({"return": [{"name": "x"}], "id": "libvirt-7"}))

    assert reply.value == [{"name": "x"}]
    assert reply.reply_id == "libvirt-7"
    assert reply.error_class is None
    assert not reply.absent


def test_an_error_reply_carries_class_and_description() -> None:
    raw = json.dumps({"error": {"class": "GenericError", "desc": "net dump: can't open /p"}})
    reply = parse_qmp_reply(raw)

    assert reply.error_class == "GenericError"
    assert reply.description == "net dump: can't open /p"
    assert reply.value is None
    assert not reply.absent


@pytest.mark.parametrize(
    "error",
    [
        {"class": "DeviceNotFound", "desc": "gone"},
        {"class": "GenericError", "desc": "object 'kdive-dump-J' not found"},
        {"class": "GenericError", "desc": "Device 'x' Not Found"},
    ],
)
def test_absence_is_recognized_from_the_class_or_the_description(error: dict) -> None:
    assert parse_qmp_reply(json.dumps({"error": error})).absent


def test_a_missing_description_reads_as_empty() -> None:
    reply = parse_qmp_reply(json.dumps({"error": {"class": "GenericError"}}))

    assert reply.description == ""
    assert not reply.absent


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"id": "libvirt-1"}',
        '{"return": {}, "error": {"class": "GenericError"}}',
        '{"error": "denied"}',
        '{"error": {"desc": "no class"}}',
        '{"error": {"class": 7}}',
    ],
)
def test_any_other_shape_is_malformed(raw: str) -> None:
    with pytest.raises(MalformedQmpReplyError):
        parse_qmp_reply(raw)
