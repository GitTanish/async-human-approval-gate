from __future__ import annotations

import re
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
from approval_gate.cli import main
from approval_gate.gate import ApprovalGate
from approval_gate.models import Request, Status
from approval_gate.stores import SqliteStore


@dataclass
class CliEnv:
    db: Path
    clock: ManualClock
    gate: ApprovalGate
    store: SqliteStore

    def run(
        self,
        *arguments: str,
        input_fn=None,
        tty: bool | None = None,
    ) -> int:
        return main(
            ["--db", str(self.db), *arguments],
            clock=self.clock,
            input_fn=input_fn,
            tty=tty,
        )

    def propose(
        self,
        *,
        workflow_id: str = "wf_1",
        ttl: timedelta = timedelta(seconds=60),
        action: dict[str, Any] | None = None,
        summary: str = "Send the customer email",
    ) -> Request:
        return self.gate.propose(
            workflow_id=workflow_id,
            action=action or {"tool": "send_email", "to": "a@b.c"},
            summary=summary,
            requested_by="agent:planner",
            ttl=ttl,
        ).request


@pytest.fixture
def cli(tmp_path) -> CliEnv:
    clock = ManualClock(1_000_000)
    store = SqliteStore(tmp_path / "cli.sqlite3", clock=clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=SeqIds(),
        sync_sleeper=FakeSyncSleeper(clock),
        async_sleeper=FakeAsyncSleeper(clock),
    )
    try:
        yield CliEnv(
            db=tmp_path / "cli.sqlite3",
            clock=clock,
            gate=gate,
            store=store,
        )
    finally:
        store.close()


def test_list_sweeps_overdue_requests_then_prints_pending(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overdue = cli.propose(ttl=timedelta(seconds=1))
    pending = cli.propose(ttl=timedelta(seconds=60))
    cli.clock.advance_ms(1_000)

    assert cli.run("list") == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "swept 1"
    assert pending.id in captured.out
    assert overdue.id not in captured.out

    stored = cli.gate.get(overdue.id)
    assert stored is not None
    assert stored.status is Status.EXPIRED
    outcome = cli.gate.outcome(overdue.id)
    assert outcome is not None
    assert outcome.actor == "system:expiry"

    # The next list has nothing left to sweep.
    assert cli.run("list") == 0
    assert capsys.readouterr().out.splitlines()[0] == "swept 0"


def test_list_help_documents_its_write_side_effect(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.run("list", "--help")
    assert raised.value.code == 0
    text = capsys.readouterr().out.lower()
    assert "sweep" in text
    assert "pending" in text


def test_list_filters_by_workflow_and_limit(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = cli.propose(workflow_id="wf_a")
    cli.clock.advance_ms(1)
    second = cli.propose(workflow_id="wf_b")
    cli.clock.advance_ms(1)
    third = cli.propose(workflow_id="wf_a")

    assert cli.run("list", "--workflow", "wf_a") == 0
    output = capsys.readouterr().out
    assert third.id in output
    assert first.id in output
    assert second.id not in output

    assert cli.run("list", "--limit", "1") == 0
    output = capsys.readouterr().out
    assert third.id in output
    assert first.id not in output
    assert second.id not in output


def test_show_is_a_pure_read_that_reports_overdue(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose(ttl=timedelta(seconds=1))
    cli.clock.advance_ms(1_000)

    assert cli.run("show", request.id) == 0

    output = capsys.readouterr().out
    assert "status: pending (overdue)" in output
    assert f"action_digest: {request.action_digest}" in output
    assert '"tool": "send_email"' in output

    stored = cli.gate.get(request.id)
    assert stored is not None
    assert stored.status is Status.PENDING
    assert stored.version == 0
    assert cli.store.get_transition(request.id) is None


def test_show_prints_the_terminal_outcome(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    cli.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="key-1",
        reason="looks good",
    )

    assert cli.run("show", request.id) == 0

    output = capsys.readouterr().out
    assert "status: approved" in output
    assert "outcome:" in output
    assert "actor: alice@corp" in output
    assert "reason: looks good" in output


def test_show_missing_request_exits_2(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.run("show", "missing") == 2
    assert "not found" in capsys.readouterr().err


def test_sweep_command_expires_each_due_request_once(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = cli.propose(ttl=timedelta(seconds=1))
    second = cli.propose(ttl=timedelta(seconds=1))
    cli.clock.advance_ms(1_000)

    assert cli.run("sweep") == 0
    assert capsys.readouterr().out.strip() == "swept 2"

    for request in (first, second):
        stored = cli.gate.get(request.id)
        assert stored is not None
        assert stored.status is Status.EXPIRED
        assert stored.version == 1

    assert cli.run("sweep") == 0
    assert capsys.readouterr().out.strip() == "swept 0"


def test_approve_applies_then_replays(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    arguments = (
        "approve",
        request.id,
        "--responder",
        "alice@corp",
        "--digest",
        request.action_digest,
        "--reason",
        "looks good",
        "--idempotency-key",
        "key-1",
    )

    assert cli.run(*arguments) == 0
    first = capsys.readouterr()
    assert first.out.strip() == f"approved {request.id}"
    assert first.err == ""

    stored = cli.gate.get(request.id)
    assert stored is not None
    assert stored.status is Status.APPROVED
    assert stored.version == 1
    transition = cli.store.get_transition(request.id)
    assert transition is not None
    assert transition.actor == "alice@corp"
    assert transition.reason == "looks good"
    assert transition.idempotency_key == "key-1"

    assert cli.run(*arguments) == 0
    replay = capsys.readouterr()
    assert replay.out.strip() == f"approved {request.id} (replayed)"
    assert cli.gate.get(request.id).version == 1


def test_generated_key_is_printed_and_can_retry_a_failed_invocation(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    failed = cli.run(
        "approve",
        request.id,
        "--responder",
        "alice@corp",
        "--digest",
        "0" * 64,
    )
    assert failed == 5

    captured = capsys.readouterr()
    match = re.search(r"idempotency_key=([0-9a-f]{32})", captured.err)
    assert match is not None
    key = match.group(1)
    assert cli.gate.get(request.id).status is Status.PENDING

    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "alice@corp",
            "--digest",
            request.action_digest,
            "--idempotency-key",
            key,
        )
        == 0
    )
    capsys.readouterr()
    transition = cli.store.get_transition(request.id)
    assert transition is not None
    assert transition.idempotency_key == key


def test_reject_requires_a_non_empty_reason(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    with pytest.raises(SystemExit) as missing:
        cli.run(
            "reject",
            request.id,
            "--responder",
            "bob@corp",
            "--digest",
            request.action_digest,
        )
    assert missing.value.code == 6

    assert (
        cli.run(
            "reject",
            request.id,
            "--responder",
            "bob@corp",
            "--digest",
            request.action_digest,
            "--reason",
            "   ",
        )
        == 6
    )
    assert "non-empty" in capsys.readouterr().err
    assert cli.gate.get(request.id).status is Status.PENDING


def test_reject_applies_with_actor_and_reason(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "reject",
            request.id,
            "--responder",
            "bob@corp",
            "--digest",
            request.action_digest,
            "--reason",
            "copy is wrong",
            "--idempotency-key",
            "reject-1",
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == f"rejected {request.id}"

    outcome = cli.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.status is Status.REJECTED
    assert outcome.actor == "bob@corp"
    assert outcome.reason == "copy is wrong"


def test_cancel_applies_and_a_late_approve_exits_4(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "cancel",
            request.id,
            "--actor",
            "agent:planner",
            "--reason",
            "superseded",
            "--idempotency-key",
            "cancel-1",
        )
        == 0
    )
    capsys.readouterr()

    outcome = cli.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.status is Status.CANCELLED
    assert outcome.actor == "agent:planner"
    assert outcome.reason == "superseded"

    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "alice@corp",
            "--digest",
            request.action_digest,
            "--idempotency-key",
            "late",
        )
        == 4
    )
    error = capsys.readouterr().err
    assert "current: status=cancelled" in error


def test_already_decided_with_a_new_key_exits_3(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    cli.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="key-1",
    )

    assert (
        cli.run(
            "reject",
            request.id,
            "--responder",
            "bob@corp",
            "--digest",
            request.action_digest,
            "--reason",
            "no",
            "--idempotency-key",
            "key-2",
        )
        == 3
    )
    error = capsys.readouterr().err
    assert "current: status=approved" in error
    assert "actor=alice@corp" in error


def test_reusing_a_key_for_a_different_operation_exits_3(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    common = (
        request.id,
        "--responder",
        "alice@corp",
        "--digest",
        request.action_digest,
        "--idempotency-key",
        "shared",
    )
    assert cli.run("approve", *common, "--reason", "first") == 0
    capsys.readouterr()

    assert cli.run("approve", *common, "--reason", "changed") == 3
    error = capsys.readouterr().err
    assert "idempotency" in error.lower()
    assert cli.gate.get(request.id).version == 1


def test_expired_decision_exits_4(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose(ttl=timedelta(seconds=1))
    cli.clock.advance_ms(1_000)

    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "alice@corp",
            "--digest",
            request.action_digest,
            "--idempotency-key",
            "expired",
        )
        == 4
    )
    error = capsys.readouterr().err
    assert "current: status=expired" in error
    assert "actor=system:expiry" in error
    assert cli.gate.get(request.id).status is Status.EXPIRED


def test_digest_mismatch_exits_5_and_keeps_the_request_pending(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    wrong = "f" * 64
    if wrong == request.action_digest:
        wrong = "e" * 64

    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "alice@corp",
            "--digest",
            wrong,
            "--idempotency-key",
            "wrong",
        )
        == 5
    )
    assert "digest" in capsys.readouterr().err.lower()
    stored = cli.gate.get(request.id)
    assert stored is not None
    assert stored.status is Status.PENDING
    assert stored.version == 0


def test_invalid_input_exits_6(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    # A digest prefix is not a full digest.
    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "alice@corp",
            "--digest",
            request.action_digest[:12],
            "--idempotency-key",
            "prefix",
        )
        == 6
    )

    assert (
        cli.run(
            "approve",
            request.id,
            "--responder",
            "",
            "--digest",
            request.action_digest,
            "--idempotency-key",
            "empty-actor",
        )
        == 6
    )

    assert cli.run("list", "--limit", "-1") == 6

    for arguments in (
        ("bogus",),
        ("approve",),
        ("show",),
    ):
        with pytest.raises(SystemExit) as raised:
            cli.run(*arguments)
        assert raised.value.code == 6

    assert cli.gate.get(request.id).status is Status.PENDING
    assert cli.store.get_transition(request.id) is None


def test_missing_request_decisions_exit_2(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.run(
            "approve",
            "missing",
            "--responder",
            "alice@corp",
            "--digest",
            "0" * 64,
            "--idempotency-key",
            "missing",
        )
        == 2
    )
    assert "does not exist" in capsys.readouterr().err.lower()

    assert (
        cli.run(
            "cancel",
            "missing",
            "--actor",
            "agent:planner",
            "--idempotency-key",
            "missing-cancel",
        )
        == 2
    )
    assert "does not exist" in capsys.readouterr().err.lower()


def _answers(*values: str):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def test_review_approves_without_typing_id_or_digest(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "review",
            "--responder",
            "alice@corp",
            input_fn=_answers("1", "a", "y"),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Pending approvals" in output
    assert "Send the customer email" in output
    assert "Payload fingerprint:" in output
    assert f"approved {request.id}" in output
    assert "alice@corp" in output

    stored = cli.gate.get(request.id)
    assert stored is not None
    assert stored.status is Status.APPROVED
    transition = cli.store.get_transition(request.id)
    assert transition is not None
    assert transition.actor == "alice@corp"


def test_review_accepts_a_direct_request_id(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "review",
            request.id,
            "--responder",
            "alice@corp",
            input_fn=_answers("a", "y"),
            tty=True,
        )
        == 0
    )

    assert cli.gate.get(request.id).status is Status.APPROVED
    assert "Approval #1" in capsys.readouterr().out


def test_review_rejects_with_a_reason(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "review",
            "--responder",
            "bob@corp",
            input_fn=_answers("1", "r", "", "copy is wrong", "y"),
            tty=True,
        )
        == 0
    )

    outcome = cli.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.status is Status.REJECTED
    assert outcome.actor == "bob@corp"
    assert outcome.reason == "copy is wrong"
    assert "rejected" in capsys.readouterr().out


def test_review_skip_leaves_the_request_pending(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "review",
            "--responder",
            "alice@corp",
            input_fn=_answers("1", "s"),
            tty=True,
        )
        == 0
    )

    assert cli.gate.get(request.id).status is Status.PENDING
    assert "still pending" in capsys.readouterr().out


def test_review_no_pending_requests(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.run(
            "review",
            "--responder",
            "alice@corp",
            input_fn=_answers(),
            tty=True,
        )
        == 0
    )
    assert "no pending requests" in capsys.readouterr().out


def test_review_requires_a_terminal(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.run(
            "review",
            "--responder",
            "alice@corp",
            tty=False,
        )
        == 6
    )
    assert "interactive terminal" in capsys.readouterr().err


def test_review_detects_an_interactive_terminal_by_default(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = cli.propose()
    answers = iter(["a", "y"])
    monkeypatch.setattr(
        "sys.stdin",
        type("FakeStdin", (), {"isatty": lambda self: True})(),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert cli.run("review", request.id, "--responder", "alice@corp") == 0

    assert cli.gate.get(request.id).status is Status.APPROVED
    assert "approved" in capsys.readouterr().out


def test_review_refuses_when_stdin_is_not_a_tty_by_default(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        type("FakeStdin", (), {"isatty": lambda self: False})(),
    )

    assert cli.run("review", "--responder", "alice@corp") == 6
    assert "interactive terminal" in capsys.readouterr().err


def test_review_direct_id_sweeps_and_shows_expiry(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose(ttl=timedelta(seconds=1))
    cli.clock.advance_ms(1_000)

    assert (
        cli.run(
            "review",
            request.id,
            "--responder",
            "alice@corp",
            input_fn=_answers(),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "status: expired" in output
    assert "system:expiry" in output
    assert "will not execute" in output
    assert cli.gate.get(request.id).status is Status.EXPIRED


def test_review_details_show_the_full_digest_on_demand(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()

    assert (
        cli.run(
            "review",
            request.id,
            "--responder",
            "alice@corp",
            input_fn=_answers("?", "q"),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"action_digest: {request.action_digest}" in output
    assert '"tool":"send_email"' in output
    assert cli.gate.get(request.id).status is Status.PENDING


def test_review_escapes_terminal_control_characters(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose(
        summary="danger\x1b[31m",
        action={"to": "a\x1b[31mb", "tool": "send_email"},
    )

    assert (
        cli.run(
            "review",
            request.id,
            "--responder",
            "alice@corp",
            input_fn=_answers("q"),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\\x1b" in output


def test_review_already_decided_shows_the_receipt(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = cli.propose()
    cli.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="already",
    )

    assert (
        cli.run(
            "review",
            request.id,
            "--responder",
            "bob@corp",
            input_fn=_answers(),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "status: approved" in output
    assert "alice@corp" in output
    assert "will not execute" not in output


def test_review_list_sweeps_overdue_and_filters_workflow(
    cli: CliEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    overdue = cli.propose(workflow_id="wf_a", ttl=timedelta(seconds=1))
    pending = cli.propose(workflow_id="wf_b")
    cli.clock.advance_ms(1_000)

    assert (
        cli.run(
            "review",
            "--workflow",
            "wf_b",
            "--responder",
            "alice@corp",
            input_fn=_answers("q"),
            tty=True,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "swept 1 overdue request(s)" in output
    assert pending.id not in output
    assert "wf_b" in output
    assert cli.gate.get(overdue.id).status is Status.EXPIRED
