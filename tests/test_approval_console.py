from __future__ import annotations

from dataclasses import dataclass, field
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
from approval_gate.models import Request, Status
from approval_gate.payload import thaw_json
from approval_gate.presentation import short_reference
from approval_gate.stores import SqliteStore
from examples import approval_console


@dataclass
class ConsoleEnv:
    db: Path
    clock: ManualClock
    gate: ApprovalGate
    last_prompts: list[str] = field(default_factory=list, init=False)

    def run(self, *arguments: str, answers: tuple[str, ...]) -> int:
        iterator = iter(answers)
        self.last_prompts = []

        def input_fn(prompt: str) -> str:
            self.last_prompts.append(prompt)
            return next(iterator)

        return approval_console.main(
            ["--db", str(self.db), *arguments],
            clock=self.clock,
            input_fn=input_fn,
        )

    def propose(
        self,
        *,
        action: dict[str, Any] | None = None,
        summary: str = "Commit the console work",
        workflow_id: str = "wf_demo",
        ttl: timedelta = timedelta(seconds=60),
    ) -> Request:
        return self.gate.propose(
            workflow_id=workflow_id,
            action=action
            or {
                "tool": "git_commit",
                "message": "Polish the approval console",
                "command": "git commit -m ...",
            },
            summary=summary,
            requested_by="agent:planner",
            ttl=ttl,
        ).request


@pytest.fixture
def console(tmp_path) -> ConsoleEnv:
    clock = ManualClock(1_000_000)
    store = SqliteStore(tmp_path / "console.sqlite3", clock=clock)
    gate = ApprovalGate(
        store=store,
        clock=clock,
        ids=SeqIds(),
        sync_sleeper=FakeSyncSleeper(clock),
        async_sleeper=FakeAsyncSleeper(clock),
    )
    try:
        yield ConsoleEnv(
            db=tmp_path / "console.sqlite3",
            clock=clock,
            gate=gate,
        )
    finally:
        store.close()


def test_pending_and_show_render_a_readable_card_without_the_full_digest(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("pending", "show 1", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Approval Console" in output
    assert "Approver: alice@corp" in output
    assert "Pending approvals:" in output
    assert "Commit the console work" in output
    assert "wf_demo | agent:planner" in output
    assert "Approval #1" in output
    assert "Command: git commit -m ..." in output
    assert "Payload fingerprint:" in output
    assert "The agent is waiting" in output
    assert request.action_digest not in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_approve_by_list_number_confirms_then_records_the_human_identity(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve 1", "y", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    prompts = [prompt.strip() for prompt in console.last_prompts]
    assert 'Approve "Commit the console work"? [y/N]' in prompts
    assert f"approved {request.id}" in output
    assert "actor: alice@corp" in output
    assert "The agent may now verify and execute the approved payload." in output

    stored = console.gate.get(request.id)
    assert stored is not None
    assert stored.status is Status.APPROVED
    outcome = console.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.actor == "alice@corp"
    assert outcome.reason is None


def test_approve_without_a_target_uses_the_only_pending_request(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve", "yes", "quit"),
        )
        == 0
    )

    capsys.readouterr()
    assert console.gate.get(request.id).status is Status.APPROVED


def test_approve_accepts_a_full_request_id(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=(f"approve {request.id}", "y", "quit"),
        )
        == 0
    )

    capsys.readouterr()
    assert console.gate.get(request.id).status is Status.APPROVED


def test_approve_accepts_the_short_reference_shown_in_the_inbox(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()
    reference = short_reference(request.id)

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=(f"approve {reference}", "y", "quit"),
        )
        == 0
    )

    capsys.readouterr()
    assert console.gate.get(request.id).status is Status.APPROVED


def test_reject_accepts_a_short_reference_and_reason(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()
    reference = short_reference(request.id)

    assert (
        console.run(
            "--as",
            "bob@corp",
            answers=(f"reject {reference} wrong branch", "y", "quit"),
        )
        == 0
    )

    capsys.readouterr()
    outcome = console.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.status is Status.REJECTED
    assert outcome.reason == "wrong branch"


def test_unknown_short_reference_does_not_fall_back(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve #DEADBEEF", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "No request with short reference" in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_approve_can_be_declined_without_changing_the_request(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve 1", "n", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Not approved." in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_reject_accepts_an_inline_reason(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "bob@corp",
            answers=("reject 1 copy is wrong", "y", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"rejected {request.id}" in output
    assert "actor: bob@corp" in output
    assert "reason: copy is wrong" in output
    assert "The agent will not execute this action." in output

    outcome = console.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.status is Status.REJECTED
    assert outcome.actor == "bob@corp"
    assert outcome.reason == "copy is wrong"


def test_reject_prompts_until_the_reason_is_non_empty(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "bob@corp",
            answers=("reject 1", "", "copy is wrong", "y", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "A rejection needs a non-empty reason." in output
    outcome = console.gate.outcome(request.id)
    assert outcome is not None
    assert outcome.reason == "copy is wrong"


def test_details_shows_the_full_digest_and_canonical_payload(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("details 1", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"request_id: {request.id}" in output
    assert f"action_digest: {request.action_digest}" in output
    assert '{"command":"git commit -m ..."' in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_pending_sweeps_overdue_requests(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose(ttl=timedelta(seconds=1))
    console.clock.advance_ms(1_000)

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("pending", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Swept 1 expired request(s)." in output
    assert "No pending approvals." in output
    assert console.gate.get(request.id).status is Status.EXPIRED


def test_show_sweeps_an_overdue_request_and_prints_the_expiry_receipt(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose(ttl=timedelta(seconds=1))
    console.clock.advance_ms(1_000)

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=(f"show {request.id}", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "status: expired" in output
    assert "actor: system:expiry" in output
    assert "The agent will not execute this action." in output
    assert "The agent is waiting" not in output
    assert console.gate.get(request.id).status is Status.EXPIRED


def test_approving_an_already_decided_request_shows_the_receipt(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()
    console.gate.approve(
        request.id,
        responder="alice@corp",
        expected_digest=request.action_digest,
        idempotency_key="already",
        reason="looks good",
    )

    assert (
        console.run(
            "--as",
            "bob@corp",
            answers=(f"approve {request.id}", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "status: approved" in output
    assert "actor: alice@corp" in output
    assert "reason: looks good" in output
    assert "The agent may now verify" in output
    assert "The agent is waiting" not in output
    assert console.gate.outcome(request.id).actor == "alice@corp"


def test_unknown_target_never_falls_back_to_the_only_pending_request(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve not-a-request", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "was not found" in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_multiple_words_as_a_target_are_rejected_before_lookup(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve two words", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "A target must be one number or request ID." in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_out_of_range_numbers_do_not_select_a_request(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose()

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("approve 0", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Choose a number from 1 to 1." in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_numbered_approval_uses_the_order_shown_in_the_inbox(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = console.propose(summary="First action")
    console.clock.advance_ms(1)
    second = console.propose(summary="Second action")

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("pending", "approve 1", "y", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "First action" in output
    assert "Second action" in output
    assert console.gate.get(second.id).status is Status.APPROVED
    assert console.gate.get(first.id).status is Status.PENDING


def test_workflow_filter_limits_the_inbox(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = console.propose(workflow_id="wf_a", summary="Action A")
    second = console.propose(workflow_id="wf_b", summary="Action B")

    assert (
        console.run(
            "--as",
            "alice@corp",
            "--workflow",
            "wf_b",
            answers=("pending", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Action B" in output
    assert "wf_b" in output
    assert "Action A" not in output
    assert first.id not in output
    assert second.id not in output


def test_control_characters_in_the_payload_cannot_drive_the_terminal(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = console.propose(
        summary="Commit\x1b[31m now",
        action={
            "tool": "git_commit",
            "message": "a\x1b[31mb",
            "command": "git commit\nspoofed",
        },
    )

    assert (
        console.run(
            "--as",
            "alice@corp",
            answers=("show 1", "quit"),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\\x1b" in output
    # The newline inside a block is indented, so it cannot become a bare
    # top-level console line.
    assert "\nspoofed" not in output
    assert console.gate.get(request.id).status is Status.PENDING


def test_console_exits_cleanly_on_eof(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def input_fn(_prompt: str) -> str:
        raise EOFError

    assert (
        approval_console.main(
            ["--db", str(console.db), "--as", "alice@corp"],
            clock=console.clock,
            input_fn=input_fn,
        )
        == 0
    )
    assert "Goodbye." in capsys.readouterr().out


def test_as_identity_is_required(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        approval_console.main(
            ["--db", str(console.db)],
            clock=console.clock,
        )
    assert raised.value.code == 6
    assert "--as" in capsys.readouterr().err


def test_empty_as_identity_is_rejected(
    console: ConsoleEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        approval_console.main(
            ["--db", str(console.db), "--as", "   "],
            clock=console.clock,
        )
    assert raised.value.code == 6
    assert "non-empty identity" in capsys.readouterr().err


def test_console_does_not_reach_into_the_action_after_storage(
    console: ConsoleEnv,
) -> None:
    action = {"tool": "git_commit", "message": "original"}
    request = console.propose(action=action)
    action["message"] = "changed"
    assert thaw_json(request.action)["message"] == "original"
