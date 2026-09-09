from pathlib import Path

import pytest

from trader_api_examples.safety import (
    Journal,
    JournalState,
    LiveExecutionBlocked,
    LiveRiskState,
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
        entry_time_ms=1_700_000_000_000,
        stop_price=1985.25,
    )

    loaded = Journal.load(path)

    assert journal.state is JournalState.PENDING_SUBMISSION
    assert loaded.client_order_id == 123
    assert loaded.order_ref is None
    assert loaded.entry_time_ms == 1_700_000_000_000
    assert loaded.stop_price == 1985.25


def test_live_risk_state_persists_and_latches_drawdown(tmp_path: Path) -> None:
    path = tmp_path / "risk.json"
    state = LiveRiskState.initialize(
        path=path,
        account_fingerprint="account-1234",
        contract="LLG",
        utc_date="2026-01-05",
        equity=1000.0,
    )

    updated = state.with_updates(drawdown_triggered=True)
    loaded = LiveRiskState.load(path)

    assert updated.drawdown_triggered is True
    assert loaded.drawdown_triggered is True
    assert loaded.daily_start_equity == 1000.0
    assert loaded.peak_equity == 1000.0


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
