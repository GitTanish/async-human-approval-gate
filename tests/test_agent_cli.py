from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from approval_gate.clock import (
    FakeAsyncSleeper,
    FakeSyncSleeper,
    ManualClock,
    SeqIds,
)
from approval_gate.gate import ApprovalGate
from approval_gate.models import Outcome, Request, Status
from approval_gate.payload import thaw_json
from approval_gate.stores import SqliteStore
from examples import agent_cli


@dataclass
class AgentCliEnv:
    db: Path
    clock: ManualClock
    ids: SeqIds
    gate: ApprovalGate
    sleeper: FakeSyncSleeper
    async_sleeper: FakeAsyncSleeper

    def run(self, *arguments: str) -> int:
        return agent_cli.main(
            ["--db", str(self.db), *arguments],
            clock=self.clock,
            ids=self.ids,
            sync_sleeper=self.sleeper,
            async_sleeper=self.async_sleeper,
        )

    def get(self, request_id: str) -> Request | None:
        return self.gate.get(request_id)

    def outcome(self, request_id: str) -> Outcome | None:
        return self.gate.outcome(request_id)


@pytest.fixture
def agent(tmp_path) -> AgentCliEnv:
    clock = ManualClock(1_000_000)
    ids = SeqIds()
    sleeper = FakeSyncSleeper()
    async_sleeper = FakeAsyncSleeper()
    store = SqliteStore(tmp_path / "agent.sqlite3", clock=clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=ids,
        sync_sleeper=sleeper,
        async_sleeper=async_sleeper,
    )
    try:
        yield AgentCliEnv(
            db=tmp_path / "agent.sqlite3",
            clock=clock,
            ids=ids,
            gate=gate,
            sleeper=sleeper,
            async_sleeper=async_sleeper,
        )
    finally:
        store.close()


def propose(
    agent: AgentCliEnv,
    *,
    action: str = '{"tool":"git_commit","message":"Polish the console"}',
    summary: str = "Commit the console work",
    ttl: str = "60",
    extra: tuple[str, ...] = (),
) -> int:
    return agent.run(
        "propose",
        "--workflow",
        "wf_demo",
        "--summary",
        summary,
        "--action",
        action,
        "--ttl-seconds",
        ttl,
        *extra,
    )


def test_propose_persists_a_pending_request_and_returns_immediately(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent) == 0

    output = capsys.readouterr()
    assert "request_id=req_1" in output.out
    assert "short_ref=#1" in output.out
    assert "status=pending" in output.out
    assert "summary=Commit the console work" in output.out
    assert "workflow=wf_demo" in output.out
    assert "requested_by=agent:example" in output.out
    assert "expires=" in output.out
    assert output.err == ""

    request = agent.get("req_1")
    assert request is not None
    assert request.status is Status.PENDING
    assert request.workflow_id == "wf_demo"
    assert request.requested_by == "agent:example"
    assert thaw_json(request.action) == {
        "message": "Polish the console",
        "tool": "git_commit",
    }
    assert request.expires_at_ms == 1_060_000
    assert agent.sleeper.sleeps == []


def test_propose_json_is_machine_readable(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent, extra=("--json",)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["request_id"] == "req_1"
    assert payload["short_ref"] == "#1"
    assert payload["status"] == "pending"
    assert payload["workflow_id"] == "wf_demo"
    assert payload["deduplicated"] is False
    assert payload["expires_at_ms"] == 1_060_000
    assert len(payload["action_digest"]) == 64


def test_propose_reads_an_action_file(
    agent: AgentCliEnv,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    action_file = tmp_path / "action.json"
    action_file.write_text(
        json.dumps({"tool": "send_email", "to": "a@b.c"}),
        encoding="utf-8-sig",
    )

    assert (
        agent.run(
            "propose",
            "--workflow",
            "wf_demo",
            "--summary",
            "Send the email",
            "--action-file",
            str(action_file),
        )
        == 0
    )
    capsys.readouterr()

    request = agent.get("req_1")
    assert request is not None
    assert thaw_json(request.action)["to"] == "a@b.c"


def test_propose_reuses_a_pending_dedupe_key(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent, extra=("--dedupe-key", "commit-1")) == 0
    capsys.readouterr()

    assert propose(agent, extra=("--dedupe-key", "commit-1", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["request_id"] == "req_1"
    assert payload["deduplicated"] is True


@pytest.mark.parametrize(
    "action",
    [
        "not json",
        "[1, 2]",
        '{"tool": }',
    ],
)
def test_propose_rejects_invalid_action_json(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    assert propose(agent, action=action) == 7
    assert "error:" in capsys.readouterr().err
    assert agent.get("req_1") is None


@pytest.mark.parametrize("ttl", ["0", "-1", "nan", "inf"])
def test_propose_rejects_invalid_ttl_without_creating_a_request(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
    ttl: str,
) -> None:
    assert propose(agent, ttl=ttl) == 7
    assert "--ttl-seconds" in capsys.readouterr().err
    assert agent.get("req_1") is None


def test_propose_rejects_a_missing_action_file(
    agent: AgentCliEnv,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        agent.run(
            "propose",
            "--workflow",
            "wf_demo",
            "--summary",
            "Do something",
            "--action-file",
            str(tmp_path / "missing.json"),
        )
        == 7
    )
    assert "cannot read action JSON" in capsys.readouterr().err


def test_status_reports_pending_then_the_verified_approved_payload(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent) == 0
    capsys.readouterr()
    request = agent.get("req_1")
    assert request is not None

    assert agent.run("status", request.id) == 6
    pending = capsys.readouterr().out
    assert "pending req_1" in pending
    assert "verified" not in pending

    agent.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="human-1",
    )

    assert agent.run("status", request.id) == 0
    approved = capsys.readouterr().out
    assert "approved req_1" in approved
    assert "actor=alice@corp" in approved
    assert "verified=true" in approved
    assert 'canonical_json={"message":"Polish the console"' in approved


def test_status_json_contains_the_action_only_after_verification(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent, extra=("--json",)) == 0
    capsys.readouterr()

    assert agent.run("status", "req_1", "--json") == 6
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "pending"
    assert pending["verified"] is False
    assert pending["action"] is None

    request = agent.get("req_1")
    assert request is not None
    agent.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="human-1",
    )

    assert agent.run("status", "req_1", "--json") == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"
    assert approved["verified"] is True
    assert approved["action"] == {
        "message": "Polish the console",
        "tool": "git_commit",
    }
    assert approved["canonical_json"] == (
        '{"message":"Polish the console","tool":"git_commit"}'
    )


def test_status_reports_missing_requests(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert agent.run("status", "missing") == 2
    assert "does not exist" in capsys.readouterr().err


def test_status_exit_codes_for_rejected_expired_and_cancelled(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for index in range(3):
        assert propose(agent, extra=("--dedupe-key", f"request-{index}")) == 0
    capsys.readouterr()

    first = agent.get("req_1")
    second = agent.get("req_2")
    third = agent.get("req_3")
    assert first is not None and second is not None and third is not None

    agent.gate.reject(
        first.id,
        responder="alice@corp",
        expected_digest=first.action_digest,
        idempotency_key="reject-1",
        reason="wrong branch",
    )
    agent.gate.cancel(
        second.id,
        actor="agent:example",
        idempotency_key="cancel-1",
        reason="superseded",
    )
    agent.clock.advance_ms(61_000)
    agent.gate.sweep()

    assert agent.run("status", first.id) == 3
    assert "rejected req_1" in capsys.readouterr().out

    assert agent.run("status", second.id) == 5
    assert "cancelled req_2" in capsys.readouterr().out

    assert agent.run("status", third.id) == 4
    assert "expired req_3" in capsys.readouterr().out


def test_wait_returns_pending_when_the_callers_timeout_is_zero(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent) == 0
    capsys.readouterr()

    assert agent.run("wait", "req_1", "--timeout", "0") == 6
    output = capsys.readouterr().out
    assert "pending req_1" in output
    assert agent.sleeper.sleeps == []


def test_wait_returns_an_existing_approval(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent) == 0
    capsys.readouterr()
    request = agent.get("req_1")
    assert request is not None
    agent.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="human-1",
    )

    assert agent.run("wait", "req_1", "--timeout", "10") == 0
    output = capsys.readouterr().out
    assert "approved req_1" in output
    assert "verified=true" in output
    assert agent.sleeper.sleeps == []


def test_wait_expires_an_overdue_request(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent, ttl="1") == 0
    capsys.readouterr()
    agent.clock.advance_ms(1_000)

    assert agent.run("wait", "req_1", "--timeout", "10") == 4
    output = capsys.readouterr().out
    assert "expired req_1" in output
    assert "actor=system:expiry" in output
    assert agent.get("req_1").status is Status.EXPIRED


def test_wait_reports_missing_and_invalid_arguments(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert agent.run("wait", "missing", "--timeout", "0") == 2
    assert "does not exist" in capsys.readouterr().err

    assert propose(agent) == 0
    capsys.readouterr()

    assert agent.run("wait", "req_1", "--timeout", "nan") == 7
    assert "timeout" in capsys.readouterr().err

    assert agent.run("wait", "req_1", "--poll-interval", "0") == 7
    assert "poll_interval" in capsys.readouterr().err


def test_agent_output_escapes_terminal_control_characters(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(
        agent,
        summary="Commit\x1b[31m now",
        action='{"tool":"git_commit","message":"a\\u001bb"}',
    ) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\\x1b" in output


def test_unknown_ttl_values_are_reported_before_gate_validation(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent, ttl="1e999") == 7
    assert "positive finite" in capsys.readouterr().err


def test_propose_requires_an_action(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        agent.run(
            "propose",
            "--workflow",
            "wf_demo",
            "--summary",
            "No action",
        )
    assert raised.value.code == 7
    assert "required" in capsys.readouterr().err


def test_request_action_is_a_snapshot_not_the_argument(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    action: dict[str, Any] = {"tool": "git_commit", "message": "first"}
    assert (
        agent.run(
            "propose",
            "--workflow",
            "wf_demo",
            "--summary",
            "Commit",
            "--action",
            json.dumps(action),
        )
        == 0
    )
    capsys.readouterr()
    action["message"] = "changed"
    request = agent.get("req_1")
    assert request is not None
    assert thaw_json(request.action)["message"] == "first"


def test_wait_uses_the_injected_sleeper_when_a_decision_arrives(
    agent: AgentCliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert propose(agent) == 0
    capsys.readouterr()
    request = agent.get("req_1")
    assert request is not None

    # The fake sleeper advances the manual clock.  Approve from the callback
    # after the first poll so wait observes a real state transition without
    # sleeping on the wall clock.
    original_sleep = agent.sleeper.sleep

    def sleep_and_approve(seconds: float) -> None:
        original_sleep(seconds)
        agent.gate.approve(
            request.id,
            responder="alice@corp",
            expected_digest=request.action_digest,
            idempotency_key="human-1",
        )

    agent.sleeper.sleep = sleep_and_approve  # type: ignore[method-assign]

    assert (
        agent.run(
            "wait",
            "req_1",
            "--timeout",
            "10",
            "--poll-interval",
            "1",
        )
        == 0
    )
    assert "approved req_1" in capsys.readouterr().out
    assert agent.sleeper.sleeps == [1.0]
