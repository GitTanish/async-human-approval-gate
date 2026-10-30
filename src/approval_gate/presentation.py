"""Human-readable presentation helpers shared by the CLI and demo UI.

The core never depends on these helpers.  They only turn an already-verified
request into labels and values that a person can review without reading a
canonical JSON blob or a SHA-256 digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import Request
from .payload import FrozenJSON, thaw_json

__all__ = [
    "ActionRow",
    "action_rows",
    "canonical_json_text",
    "digest_fingerprint",
    "format_remaining",
    "format_utc",
    "one_line",
    "safe_terminal",
    "short_reference",
]


@dataclass(frozen=True, slots=True)
class ActionRow:
    """One human-readable field from an approval payload."""

    label: str
    value: str
    block: bool = False


# Ordered so the most useful parts of a tool call appear first.
_LABELS: tuple[tuple[str, str], ...] = (
    ("tool", "Tool"),
    ("method", "Method"),
    ("command", "Command"),
    ("branch", "Branch"),
    ("remote", "Remote"),
    ("path", "Path"),
    ("file", "File"),
    ("files", "Files"),
    ("url", "URL"),
    ("to", "To"),
    ("recipient", "Recipient"),
    ("from", "From"),
    ("cc", "CC"),
    ("bcc", "BCC"),
    ("subject", "Subject"),
    ("amount", "Amount"),
    ("currency", "Currency"),
    ("query", "Query"),
    ("headers", "Headers"),
    ("body", "Body"),
    ("content", "Content"),
    ("message", "Message"),
    ("diff", "Diff"),
    ("patch", "Patch"),
    ("plan", "Plan"),
    ("steps", "Steps"),
    ("description", "Description"),
    ("reason", "Reason"),
)

_BLOCK_KEYS = frozenset(
    {
        "body",
        "content",
        "description",
        "diff",
        "files",
        "headers",
        "message",
        "patch",
        "plan",
        "steps",
    },
)


def short_reference(request_id: str) -> str:
    """Return a short, non-authoritative reference for conversation."""

    raw = request_id[4:] if request_id.startswith("req_") else request_id
    raw = raw[:8]
    return f"#{raw.upper()}" if raw else "#?"


def digest_fingerprint(digest: str) -> str:
    """Return a short fingerprint; the full digest remains the authority."""

    return digest[:8] if digest else "?"


def action_rows(action: FrozenJSON) -> list[ActionRow]:
    """Render an action as recognizable labels plus a JSON catch-all.

    Known fields such as ``tool``, ``to``, and ``command`` get friendly
    labels. Everything else is preserved in an ``Other fields`` block so the
    view never hides part of the payload the digest commits to.
    """

    data = thaw_json(action)
    if not isinstance(data, dict):
        return [ActionRow("Payload", _pretty(data), True)]

    rows: list[ActionRow] = []
    used: set[str] = set()
    for key, label in _LABELS:
        if key not in data:
            continue
        value = data[key]
        rows.append(
            ActionRow(
                label=label,
                value=_format_value(value),
                block=_is_block(key, value),
            ),
        )
        used.add(key)

    remaining = {
        key: value for key, value in data.items() if key not in used
    }
    if remaining:
        rows.append(ActionRow("Other fields", _pretty(remaining), True))
    if not rows:
        rows.append(ActionRow("Payload", _pretty(data), True))
    return rows


def canonical_json_text(request: Request) -> str:
    """Return the exact stored canonical payload for a details view."""

    return request.canonical_json.decode("utf-8")


def safe_terminal(text: str) -> str:
    """Escape control characters so payload text cannot drive the terminal."""

    escaped: list[str] = []
    for character in text:
        if character in "\n\t" or character.isprintable():
            escaped.append(character)
        elif ord(character) <= 0xFF:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(f"\\u{ord(character):04x}")
    return "".join(escaped)


def one_line(text: str) -> str:
    """Render untrusted text for a single-line terminal field."""

    return safe_terminal(text).replace("\n", " ").replace("\t", " ")


def format_utc(ms: int | None) -> str:
    """Format an epoch-millisecond timestamp for a human."""

    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1_000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC",
    )


def format_remaining(now_ms: int, expires_at_ms: int) -> str:
    """Describe the time left before a request expires."""

    remaining_ms = expires_at_ms - now_ms
    if remaining_ms <= 0:
        return "expired"
    seconds = remaining_ms // 1_000
    if seconds < 60:
        return f"in {seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"in {minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"in {hours}h {minutes % 60}m"
    return f"in {hours // 24}d {hours % 24}h"


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value if value else "(empty)"
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return _pretty(value)


def _is_block(key: str, value: Any) -> bool:
    if key in _BLOCK_KEYS:
        return True
    if isinstance(value, (dict, list, tuple)):
        return True
    return isinstance(value, str) and "\n" in value


def _pretty(value: Any) -> str:
    return json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
