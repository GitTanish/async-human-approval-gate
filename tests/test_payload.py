from __future__ import annotations

import hashlib
import json
from types import MappingProxyType

import pytest

from approval_gate.errors import IntegrityError, InvalidRequest
from approval_gate.payload import (
    MAX_ACTION_BYTES,
    canonicalize_action,
    freeze_canonical_action,
    is_frozen_json,
    sha256_hex,
    thaw_json,
)


def test_canonical_bytes_sort_keys_and_use_compact_separators() -> None:
    canonical = canonicalize_action({"b": 2, "a": [True, None]})
    assert canonical == b'{"a":[true,null],"b":2}'


def test_digest_is_over_the_exact_canonical_bytes() -> None:
    canonical = canonicalize_action({"text": "hello"})
    assert sha256_hex(canonical) == hashlib.sha256(canonical).hexdigest()
    assert len(sha256_hex(canonical)) == 64


def test_non_ascii_is_preserved_without_escapes() -> None:
    canonical = canonicalize_action({"name": "café"})
    assert canonical == '{"name":"café"}'.encode("utf-8")


def test_unicode_normalization_is_not_applied() -> None:
    composed = canonicalize_action({"name": "caf\u00e9"})
    decomposed = canonicalize_action({"name": "cafe\u0301"})
    assert composed != decomposed
    assert sha256_hex(composed) != sha256_hex(decomposed)


def test_tuple_values_become_arrays() -> None:
    assert canonicalize_action({"items": (1, 2)}) == b'{"items":[1,2]}'


def test_mapping_subclasses_are_copied_to_plain_json() -> None:
    class CustomMapping(dict[str, int]):
        pass

    assert canonicalize_action(CustomMapping({"a": 1})) == b'{"a":1}'


@pytest.mark.parametrize("action", [[1, 2], "text", 1, None, True])
def test_top_level_must_be_an_object(action: object) -> None:
    with pytest.raises(InvalidRequest, match="JSON object"):
        canonicalize_action(action)  # type: ignore[arg-type]


def test_non_string_keys_are_rejected_at_the_top_level() -> None:
    with pytest.raises(InvalidRequest, match="non-string key"):
        canonicalize_action({1: "value"})  # type: ignore[dict-item]


def test_non_string_keys_are_rejected_when_nested() -> None:
    with pytest.raises(InvalidRequest, match="non-string key"):
        canonicalize_action({"outer": {2: "value"}})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(InvalidRequest, match="non-finite"):
        canonicalize_action({"value": value})


@pytest.mark.parametrize("value", [{1, 2}, b"bytes", object(), 1 + 2j])
def test_unsupported_types_are_rejected(value: object) -> None:
    with pytest.raises(InvalidRequest, match="unsupported type"):
        canonicalize_action({"value": value})


def test_circular_references_are_rejected() -> None:
    action: dict[str, object] = {}
    action["self"] = action
    with pytest.raises(InvalidRequest, match="circular"):
        canonicalize_action(action)


def test_unpaired_surrogates_are_rejected() -> None:
    with pytest.raises(InvalidRequest):
        canonicalize_action({"value": "\ud800"})


def test_size_limit_accepts_exactly_64_kib() -> None:
    padding = "a" * (MAX_ACTION_BYTES - len('{"s":""}'))
    canonical = canonicalize_action({"s": padding})
    assert len(canonical) == MAX_ACTION_BYTES


def test_size_limit_rejects_one_byte_over() -> None:
    padding = "a" * (MAX_ACTION_BYTES - len('{"s":""}') + 1)
    with pytest.raises(InvalidRequest, match="limit"):
        canonicalize_action({"s": padding})


def test_frozen_snapshot_is_deeply_immutable() -> None:
    canonical = canonicalize_action(
        {"nested": {"list": [1, {"deep": "value"}]}},
    )
    action = freeze_canonical_action(canonical)

    assert is_frozen_json(action)
    assert isinstance(action, MappingProxyType)
    nested = action["nested"]
    assert isinstance(nested, MappingProxyType)
    assert isinstance(nested["list"], tuple)
    assert isinstance(nested["list"][1], MappingProxyType)

    with pytest.raises(TypeError):
        action["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["list"][0] = 2  # type: ignore[index]


def test_frozen_snapshot_round_trips_to_exact_canonical_bytes() -> None:
    canonical = canonicalize_action(
        {"a": [1, 2.5, False, None], "unicode": "héllo"},
    )
    action = freeze_canonical_action(canonical)

    rebuilt = canonicalize_action(thaw_json(action))
    assert rebuilt == canonical
    assert sha256_hex(rebuilt) == sha256_hex(canonical)


def test_thaw_returns_a_fresh_mutable_copy() -> None:
    canonical = canonicalize_action({"nested": {"items": [1, 2]}})
    action = freeze_canonical_action(canonical)

    thawed = thaw_json(action)
    assert thawed == {"nested": {"items": [1, 2]}}
    thawed["nested"]["items"].append(3)

    assert thaw_json(action) == {"nested": {"items": [1, 2]}}
    assert canonicalize_action(thawed) != canonical


def test_caller_mutation_after_canonicalization_has_no_effect() -> None:
    source = {"nested": {"value": 1}}
    canonical = canonicalize_action(source)
    action = freeze_canonical_action(canonical)

    source["nested"]["value"] = 2
    source["new"] = True

    assert canonical == b'{"nested":{"value":1}}'
    assert thaw_json(action) == {"nested": {"value": 1}}


@pytest.mark.parametrize(
    "tampered",
    [
        b'{"a": 1}',
        b'{"b":1,"a":2}',
        b'{"a":1,"a":2}',
        b'{"s":"\\u00e9"}',
        b"[]",
        b"null",
        b"{",
        b'{"a":1} trailing',
        b"\xff\xfe",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
    ],
)
def test_noncanonical_or_invalid_stored_bytes_are_integrity_errors(
    tampered: bytes,
) -> None:
    with pytest.raises(IntegrityError):
        freeze_canonical_action(tampered)


def test_frozen_snapshot_thaws_to_json_parsed_structure() -> None:
    payload = {"a": [1, 2, 3], "b": {"c": "d"}, "e": None}
    canonical = canonicalize_action(payload)
    action = freeze_canonical_action(canonical)
    assert thaw_json(action) == json.loads(canonical)
