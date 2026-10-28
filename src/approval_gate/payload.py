"""Canonical action payloads and immutable JSON snapshots.

The canonical bytes are the authorization boundary: the digest a human
approves is computed from these exact bytes, and those same bytes are stored
and re-verified before execution.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeAlias

from .errors import IntegrityError, InvalidRequest

__all__ = [
    "FrozenJSON",
    "MAX_ACTION_BYTES",
    "canonicalize_action",
    "sha256_hex",
    "freeze_canonical_action",
    "thaw_json",
    "is_frozen_json",
]

FrozenJSON: TypeAlias = (
    Mapping[str, "FrozenJSON"]
    | tuple["FrozenJSON", ...]
    | str
    | int
    | float
    | bool
    | None
)

MAX_ACTION_BYTES = 64 * 1024


def canonicalize_action(
    action: Mapping[str, Any],
    *,
    max_bytes: int = MAX_ACTION_BYTES,
) -> bytes:
    """Validate a top-level JSON object and return its canonical UTF-8 bytes.

    Keys are sorted, separators are compact, non-ASCII text is preserved, and
    NaN/infinity are rejected.  Non-string object keys and values that are not
    JSON-compatible are rejected rather than coerced.
    """

    if not isinstance(action, Mapping):
        raise InvalidRequest("action must be a JSON object")

    try:
        normalized = _normalize(action, "$", set())
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except InvalidRequest:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidRequest(f"action is not canonical JSON: {exc}") from exc

    if len(canonical) > max_bytes:
        raise InvalidRequest(
            f"action JSON is {len(canonical)} bytes; the limit is {max_bytes}",
        )
    return canonical


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""

    return hashlib.sha256(data).hexdigest()


def freeze_canonical_action(canonical_json: bytes) -> FrozenJSON:
    """Parse stored canonical bytes and return an immutable JSON snapshot.

    This is a verification helper, not a trust boundary by itself.  It
    requires the bytes to be valid UTF-8 JSON for an object and to be exactly
    reproducible by canonicalization.  Any deviation raises ``IntegrityError``.
    """

    if not isinstance(canonical_json, bytes):
        raise IntegrityError("stored action must be bytes")

    try:
        text = canonical_json.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError("stored action is not valid UTF-8") from exc

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise IntegrityError("stored action is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise IntegrityError("stored action is not a JSON object")

    # Reject non-finite constants that the JSON parser accepts by default.
    if not _has_only_finite_numbers(parsed):
        raise IntegrityError("stored action contains a non-finite number")

    try:
        rebuilt = canonicalize_action(parsed)
    except InvalidRequest as exc:
        raise IntegrityError("stored action is not canonical") from exc

    if rebuilt != canonical_json:
        raise IntegrityError("stored action does not round-trip canonically")

    return _freeze(parsed)


def thaw_json(value: FrozenJSON) -> Any:
    """Return a fresh mutable copy of a frozen JSON snapshot."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def is_frozen_json(value: Any) -> bool:
    """Return true when ``value`` is a fully frozen JSON structure."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, MappingProxyType):
        return all(
            isinstance(key, str) and is_frozen_json(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return all(is_frozen_json(item) for item in value)
    return False


def _normalize(value: Any, path: str, seen: set[int]) -> Any:
    """Copy caller input into plain JSON structures without retaining it."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRequest(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise InvalidRequest(f"{path} is a circular reference")
        seen.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InvalidRequest(f"{path} has a non-string key")
                result[key] = _normalize(item, f"{path}[{key!r}]", seen)
            return result
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise InvalidRequest(f"{path} is a circular reference")
        seen.add(identity)
        try:
            return [
                _normalize(item, f"{path}[{index}]", seen)
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    raise InvalidRequest(
        f"{path} has unsupported type {type(value).__name__}",
    )


def _freeze(value: Any) -> FrozenJSON:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()},
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"cannot freeze {type(value).__name__}")


def _has_only_finite_numbers(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_has_only_finite_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(_has_only_finite_numbers(item) for item in value)
    return True
