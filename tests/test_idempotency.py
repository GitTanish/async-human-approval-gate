from __future__ import annotations

from typing import Any

import pytest

from approval_gate.idempotency import operation_fingerprint
from approval_gate.state import Event

DIGEST = "a" * 64
BASE: dict[str, Any] = {
    "request_id": "req_1",
    "operation": Event.APPROVE,
    "actor": "alice@corp",
    "reason": "reviewed",
    "expected_digest": DIGEST,
}


def fingerprint(**changes: Any) -> str:
    arguments = dict(BASE)
    arguments.update(changes)
    return operation_fingerprint(**arguments)


def test_fingerprint_is_deterministic_and_hex_encoded() -> None:
    first = fingerprint()
    second = fingerprint()
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "req_2"),
        ("operation", Event.REJECT),
        ("actor", "bob@corp"),
        ("reason", "different"),
        ("reason", None),
        ("expected_digest", "b" * 64),
    ],
)
def test_every_operation_field_affects_the_fingerprint(
    field: str,
    value: object,
) -> None:
    assert fingerprint(**{field: value}) != fingerprint()


def test_cancel_omits_the_digest_precondition() -> None:
    with_digest_none = operation_fingerprint(
        request_id="req_1",
        operation=Event.CANCEL,
        actor="agent:planner",
        reason="superseded",
        expected_digest=None,
    )
    same = operation_fingerprint(
        request_id="req_1",
        operation=Event.CANCEL,
        actor="agent:planner",
        reason="superseded",
        expected_digest=None,
    )
    assert with_digest_none == same

    with pytest.raises(ValueError, match="must not include"):
        operation_fingerprint(
            request_id="req_1",
            operation=Event.CANCEL,
            actor="agent:planner",
            reason="superseded",
            expected_digest=DIGEST,
        )


def test_approve_and_reject_require_a_digest() -> None:
    for operation in (Event.APPROVE, Event.REJECT):
        for digest in (None, ""):
            with pytest.raises(ValueError, match="require"):
                operation_fingerprint(
                    request_id="req_1",
                    operation=operation,
                    actor="alice@corp",
                    reason=None,
                    expected_digest=digest,
                )


def test_expiry_is_not_a_user_operation() -> None:
    with pytest.raises(ValueError, match="not a user operation"):
        operation_fingerprint(
            request_id="req_1",
            operation=Event.EXPIRE,
            actor="system:expiry",
            reason="deadline_reached",
            expected_digest=None,
        )
