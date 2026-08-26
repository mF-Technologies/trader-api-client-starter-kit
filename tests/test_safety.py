from pathlib import Path

import pytest

from trader_api_examples.safety import (
    Journal,
    JournalState,
    LiveExecutionBlocked,
    assert_live_execution_enabled,
)


def test_live_execution_requires_environment_gate_and_execute_flag() -> None:
    with pytest.raises(LiveExecutionBlocked):
        assert_live_execution_enabled(enabled=False, execute=True)

    with pytest.raises(LiveExecutionBlocked):
        assert_live_execution_enabled(enabled=True, execute=False)

    assert_live_execution_enabled(enabled=True, execute=True)


def test_journal_records_submission_intent_before_order_reference(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = Journal.begin_submission(
        path=path,
        run_id="run-1",
        account_fingerprint="account-1234",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    )

    loaded = Journal.load(path)

    assert journal.state is JournalState.PENDING_SUBMISSION
    assert loaded.client_order_id == 123
    assert loaded.order_ref is None


def test_unresolved_journal_blocks_new_submission(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    Journal.begin_submission(
        path=path,
        run_id="run-1",
        account_fingerprint="account-1234",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    )

    with pytest.raises(LiveExecutionBlocked, match="unresolved"):
        Journal.assert_clear(path)
