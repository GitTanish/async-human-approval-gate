from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from approval_gate.clock import ManualClock
from approval_gate.models import Status
from examples.fastapi_app import create_app

AGENT = {"Authorization": "Bearer agent-token"}
ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


@pytest.fixture
def http_env(tmp_path):
    clock = ManualClock(1_000_000)
    app = create_app(
        db_path=tmp_path / "http.sqlite3",
        tokens={
            "agent-token": "agent:planner",
            "alice-token": "alice@corp",
            "bob-token": "bob@corp",
        },
        clock=clock,
    )
    with TestClient(app) as client:
        yield client, clock, app


def create_request(
    client: TestClient,
    *,
    headers: dict[str, str] = AGENT,
    summary: str = "Send the email",
    ttl_seconds: float = 60,
    dedupe_key: str | None = None,
) -> dict:
    body = {
        "workflow_id": "wf_http",
        "action": {"to": "customer@example.com", "tool": "send_email"},
        "summary": summary,
        "ttl_seconds": ttl_seconds,
        "requested_by": "mallory@corp",
    }
    if dedupe_key is not None:
        body["dedupe_key"] = dedupe_key
    response = client.post("/approvals", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["request"]


def test_authentication_is_required(http_env) -> None:
    client, _clock, _app = http_env
    assert client.get("/approvals").status_code == 401
    assert (
        client.get(
            "/approvals",
            headers={"Authorization": "Bearer nope"},
        ).status_code
        == 401
    )


def test_create_read_and_list(http_env) -> None:
    client, _clock, _app = http_env
    request = create_request(client)

    assert request["requested_by"] == "agent:planner"
    assert request["status"] == "pending"
    assert request["action"] == {
        "to": "customer@example.com",
        "tool": "send_email",
    }
    assert request["version"] == 0

    listed = client.get("/approvals", headers=ALICE)
    assert listed.status_code == 200
    assert listed.json()["swept"] == 0
    assert [item["id"] for item in listed.json()["items"]] == [request["id"]]

    shown = client.get(f"/approvals/{request['id']}", headers=ALICE)
    assert shown.status_code == 200
    assert shown.json()["request"]["id"] == request["id"]
    assert shown.json()["outcome"] is None


def test_create_validates_the_action_and_ignores_body_identity(http_env) -> None:
    client, _clock, _app = http_env
    response = client.post(
        "/approvals",
        headers=AGENT,
        json={
            "workflow_id": "wf",
            "action": [1, 2],
            "summary": "bad",
            "ttl_seconds": 60,
            "requested_by": "mallory@corp",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

    response = client.post(
        "/approvals",
        headers=AGENT,
        json={
            "workflow_id": "wf",
            "action": {},
            "summary": "",
            "ttl_seconds": 60,
        },
    )
    assert response.status_code == 400


def test_get_marks_overdue_and_list_sweeps(http_env) -> None:
    client, clock, _app = http_env
    request = create_request(client, ttl_seconds=1)
    clock.advance_ms(1_000)

    shown = client.get(f"/approvals/{request['id']}", headers=ALICE)
    assert shown.status_code == 200
    assert shown.json()["request"]["status"] == "pending"
    assert shown.json()["request"]["overdue"] is True
    assert shown.json()["outcome"] is None

    swept = client.get("/approvals", headers=ALICE)
    assert swept.json() == {"items": [], "swept": 1}

    terminal = client.get(f"/approvals/{request['id']}", headers=ALICE)
    assert terminal.json()["request"]["status"] == "expired"
    assert terminal.json()["request"]["overdue"] is False
    assert terminal.json()["outcome"]["status"] == "expired"
    assert terminal.json()["outcome"]["actor"] == "system:expiry"


def test_approve_requires_header_denies_self_approval_and_replays(
    http_env,
) -> None:
    client, _clock, _app = http_env
    request = create_request(client)

    missing_key = client.post(
        f"/approvals/{request['id']}/approve",
        headers=ALICE,
        json={"expected_digest": request["action_digest"]},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "invalid_request"

    self_approval = client.post(
        f"/approvals/{request['id']}/approve",
        headers={**AGENT, "Idempotency-Key": "self"},
        json={"expected_digest": request["action_digest"]},
    )
    assert self_approval.status_code == 403

    mismatch = client.post(
        f"/approvals/{request['id']}/approve",
        headers={**ALICE, "Idempotency-Key": "wrong"},
        json={"expected_digest": "0" * 64},
    )
    assert mismatch.status_code == 412
    assert mismatch.json()["error"]["code"] == "digest_mismatch"

    headers = {**ALICE, "Idempotency-Key": "approve-1"}
    body = {"expected_digest": request["action_digest"], "reason": "fine"}
    approved = client.post(
        f"/approvals/{request['id']}/approve",
        headers=headers,
        json=body,
    )
    assert approved.status_code == 200
    assert approved.json()["replayed"] is False
    assert approved.json()["outcome"]["status"] == "approved"
    assert approved.json()["outcome"]["actor"] == "alice@corp"

    replay = client.post(
        f"/approvals/{request['id']}/approve",
        headers=headers,
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["outcome"]["actor"] == "alice@corp"

    conflict = client.post(
        f"/approvals/{request['id']}/approve",
        headers=headers,
        json={
            "expected_digest": request["action_digest"],
            "reason": "different",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    decided = client.post(
        f"/approvals/{request['id']}/reject",
        headers={**BOB, "Idempotency-Key": "reject-1"},
        json={
            "expected_digest": request["action_digest"],
            "reason": "too late",
        },
    )
    assert decided.status_code == 409
    assert decided.json()["error"]["code"] == "already_decided"
    assert decided.json()["error"]["current"]["status"] == "approved"
    assert decided.json()["error"]["current"]["actor"] == "alice@corp"


def test_reject_requires_a_reason(http_env) -> None:
    client, _clock, _app = http_env
    request = create_request(client)

    missing = client.post(
        f"/approvals/{request['id']}/reject",
        headers={**BOB, "Idempotency-Key": "reject-1"},
        json={"expected_digest": request["action_digest"]},
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"

    blank = client.post(
        f"/approvals/{request['id']}/reject",
        headers={**BOB, "Idempotency-Key": "reject-1"},
        json={
            "expected_digest": request["action_digest"],
            "reason": "   ",
        },
    )
    assert blank.status_code == 400

    rejected = client.post(
        f"/approvals/{request['id']}/reject",
        headers={**BOB, "Idempotency-Key": "reject-2"},
        json={
            "expected_digest": request["action_digest"],
            "reason": "copy is wrong",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["outcome"]["status"] == "rejected"
    assert rejected.json()["outcome"]["reason"] == "copy is wrong"


def test_cancel_requires_the_requester(http_env) -> None:
    client, _clock, _app = http_env
    request = create_request(client)

    denied = client.post(
        f"/approvals/{request['id']}/cancel",
        headers={**ALICE, "Idempotency-Key": "cancel-1"},
        json={"reason": "not mine"},
    )
    assert denied.status_code == 403

    cancelled = client.post(
        f"/approvals/{request['id']}/cancel",
        headers={**AGENT, "Idempotency-Key": "cancel-2"},
        json={"reason": "superseded"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["outcome"]["status"] == "cancelled"
    assert cancelled.json()["outcome"]["actor"] == "agent:planner"


def test_expiry_error_carries_the_current_outcome(http_env) -> None:
    client, clock, _app = http_env
    request = create_request(client, ttl_seconds=1)
    clock.advance_ms(1_000)

    response = client.post(
        f"/approvals/{request['id']}/approve",
        headers={**ALICE, "Idempotency-Key": "expired"},
        json={"expected_digest": request["action_digest"]},
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "request_expired"
    assert response.json()["error"]["current"]["status"] == "expired"
    assert response.json()["error"]["current"]["actor"] == "system:expiry"


def test_missing_request_is_a_404_envelope(http_env) -> None:
    client, _clock, _app = http_env
    shown = client.get("/approvals/missing", headers=ALICE)
    assert shown.status_code == 404
    assert shown.json()["error"]["code"] == "request_not_found"

    approved = client.post(
        "/approvals/missing/approve",
        headers={**ALICE, "Idempotency-Key": "missing"},
        json={"expected_digest": "0" * 64},
    )
    assert approved.status_code == 404


def test_create_dedupe_and_list_workflow_filter(http_env) -> None:
    client, _clock, _app = http_env
    first = create_request(client, dedupe_key="same")
    second = client.post(
        "/approvals",
        headers=AGENT,
        json={
            "workflow_id": "wf_http",
            "action": {"different": True},
            "summary": "Other",
            "ttl_seconds": 60,
            "dedupe_key": "same",
        },
    )
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["request"]["id"] == first["id"]

    other = client.post(
        "/approvals",
        headers=AGENT,
        json={
            "workflow_id": "wf_other",
            "action": {},
            "summary": "Other workflow",
            "ttl_seconds": 60,
        },
    )
    assert other.status_code == 201

    filtered = client.get(
        "/approvals?workflow_id=wf_http",
        headers=ALICE,
    )
    assert [item["id"] for item in filtered.json()["items"]] == [first["id"]]


def test_decisions_require_the_idempotency_header_for_all_endpoints(
    http_env,
) -> None:
    client, _clock, _app = http_env
    request = create_request(client)

    for action in ("approve", "reject", "cancel"):
        if action == "cancel":
            body = {"reason": "why"}
        elif action == "reject":
            body = {
                "expected_digest": request["action_digest"],
                "reason": "why",
            }
        else:
            body = {"expected_digest": request["action_digest"]}
        response = client.post(
            f"/approvals/{request['id']}/{action}",
            headers=ALICE if action != "cancel" else AGENT,
            json=body,
        )
        assert response.status_code == 400, action
        assert response.json()["error"]["code"] == "invalid_request"
