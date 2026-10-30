from __future__ import annotations

from approval_gate.payload import canonicalize_action, freeze_canonical_action
from approval_gate.presentation import (
    action_rows,
    canonical_json_text,
    digest_fingerprint,
    format_remaining,
    format_utc,
    one_line,
    safe_terminal,
    short_reference,
)

from support import make_request


def test_short_reference_is_short_and_stable() -> None:
    assert short_reference("req_abcdef123456") == "#ABCDEF12"
    assert short_reference("abcdef123456") == "#ABCDEF12"
    assert short_reference("req_") == "#?"
    assert short_reference("") == "#?"


def test_digest_fingerprint_keeps_the_full_digest_out_of_the_card() -> None:
    digest = "0123456789abcdef" * 4
    assert digest_fingerprint(digest) == "01234567"
    assert digest_fingerprint("") == "?"


def test_format_utc_uses_epoch_milliseconds() -> None:
    assert format_utc(0) == "1970-01-01 00:00:00 UTC"
    assert format_utc(1_000) == "1970-01-01 00:00:01 UTC"
    assert format_utc(None) == "-"


def test_format_remaining_reports_each_human_scale() -> None:
    assert format_remaining(1_000, 1_000) == "expired"
    assert format_remaining(1_000, 1_999) == "in 0s"
    assert format_remaining(0, 1_000) == "in 1s"
    assert format_remaining(0, 59_000) == "in 59s"
    assert format_remaining(0, 60_000) == "in 1m"
    assert format_remaining(0, 3_600_000) == "in 1h 0m"
    assert format_remaining(0, 176_400_000) == "in 2d 1h"


def test_action_rows_labels_known_fields_and_preserves_the_rest() -> None:
    action = freeze_canonical_action(
        canonicalize_action(
            {
                "tool": "git_commit",
                "branch": "main",
                "command": "git commit -m ...",
                "message": "Polish the console\nand docs",
                "files": ["src/approval_gate/cli.py", "README.md"],
                "unknown": {"nested": True},
            },
        ),
    )

    rows = action_rows(action)

    assert [row.label for row in rows] == [
        "Tool",
        "Command",
        "Branch",
        "Files",
        "Message",
        "Other fields",
    ]
    by_label = {row.label: row for row in rows}
    assert by_label["Tool"].value == "git_commit"
    assert by_label["Branch"].value == "main"
    assert by_label["Command"].block is False
    assert by_label["Files"].block is True
    assert by_label["Message"].block is True
    assert by_label["Message"].value == "Polish the console\nand docs"
    assert '"unknown"' in by_label["Other fields"].value
    assert by_label["Other fields"].block is True


def test_action_rows_handles_an_empty_action() -> None:
    rows = action_rows(freeze_canonical_action(canonicalize_action({})))
    assert len(rows) == 1
    assert rows[0].label == "Payload"
    assert rows[0].value == "{}"
    assert rows[0].block is True


def test_action_rows_handles_non_string_values() -> None:
    action = freeze_canonical_action(
        canonicalize_action(
            {
                "tool": "send_email",
                "to": "",
                "amount": 12.5,
                "dry_run": False,
                "note": None,
            },
        ),
    )
    rows = {row.label: row.value for row in action_rows(action)}
    assert rows["To"] == "(empty)"
    assert rows["Amount"] == "12.5"
    assert rows["Other fields"].startswith("{")
    assert '"dry_run": false' in rows["Other fields"]
    assert '"note": null' in rows["Other fields"]


def test_safe_terminal_escapes_control_characters_but_keeps_layout() -> None:
    assert safe_terminal("a\x1b[31mb") == "a\\x1b[31mb"
    assert safe_terminal("line\nnext\tcell") == "line\nnext\tcell"
    assert safe_terminal("bell\x07") == "bell\\x07"
    assert safe_terminal("wide\x85") == "wide\\x85"
    assert safe_terminal("unicode\u2028") == "unicode\\u2028"


def test_one_line_removes_line_breaks_and_tabs() -> None:
    assert one_line("a\nb\tc") == "a b c"
    assert one_line("danger\x1b[31m") == "danger\\x1b[31m"


def test_canonical_json_text_is_the_stored_bytes() -> None:
    request = make_request()
    assert canonical_json_text(request) == "{}"
