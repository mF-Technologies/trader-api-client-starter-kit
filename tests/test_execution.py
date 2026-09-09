from pathlib import Path
from typing import Any

import pytest

from trader_api_examples.execution import ExecutionManager
from trader_api_examples.safety import Journal, JournalState, LiveExecutionBlocked


class FakeExecutionClient:
    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path
        self.position_open = False

    async def add_market_deal(self, **_: Any) -> str | None:
        assert Journal.load(self.journal_path).state is JournalState.PENDING_SUBMISSION
        self.position_open = True
        return "deal-42"

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self.position_open:
            return []
        return [{"ref": "deal-42", "contract": "EURUSD", "amount": 1000}]

    async def get_position_detail(self, order_ref: str) -> dict[str, Any] | None:
        assert order_ref == "deal-42"
        return {"ref": order_ref} if self.position_open else None

    async def liquidate_market_deal(self, **_: Any) -> str:
        journal = Journal.load(self.journal_path)
        assert journal.state is JournalState.CLEANUP_PENDING
        assert journal.cleanup_client_order_id == 124
        self.position_open = False
        return "liq-43"


@pytest.mark.asyncio
async def test_execution_writes_intent_then_confirms_and_cleans_up(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "execution.json"
    client = FakeExecutionClient(journal_path)
    manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
    )

    journal = await manager.open_position(
        execute=True,
        account_fingerprint="account-abcd",
        contract="EURUSD",
        amount=1000,
        buy=True,
        client_order_id=123,
        entry_time_ms=1_700_000_000_000,
        stop_price=1985.25,
        entry_price=2000.0,
        entry_equity=10000.0,
    )
    assert journal.state is JournalState.OPEN
    assert journal.order_ref == "deal-42"
    assert journal.entry_time_ms == 1_700_000_000_000
    assert journal.stop_price == 1985.25
    assert journal.entry_price == 2000.0
    assert journal.entry_equity == 10000.0

    cleanup_ref = await manager.cleanup(journal=journal, execute=True, client_order_id=124)

    assert cleanup_ref == "liq-43"
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_execution_discovers_position_when_deal_reference_is_missing(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "execution.json"
    client = FakeExecutionClient(journal_path)

    async def add_market_deal_without_reference(**_: Any) -> None:
        assert Journal.load(journal_path).state is JournalState.PENDING_SUBMISSION
        client.position_open = True
        return None

    client.add_market_deal = add_market_deal_without_reference  # type: ignore[method-assign]
    manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
    )

    journal = await manager.open_position(
        execute=True,
        account_fingerprint="account-abcd",
        contract="EURUSD",
        amount=1000,
        buy=True,
        client_order_id=123,
    )

    assert journal.order_ref == "deal-42"


@pytest.mark.asyncio
async def test_execution_blocks_duplicate_open_when_journal_exists(tmp_path: Path) -> None:
    journal_path = tmp_path / "execution.json"
    client = FakeExecutionClient(journal_path)
    manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
    )

    await manager.open_position(
        execute=True,
        account_fingerprint="account-abcd",
        contract="EURUSD",
        amount=1000,
        buy=True,
        client_order_id=123,
    )

    with pytest.raises(LiveExecutionBlocked, match="unresolved"):
        await manager.open_position(
            execute=True,
            account_fingerprint="account-abcd",
            contract="EURUSD",
            amount=1000,
            buy=True,
            client_order_id=124,
        )

    assert client.position_open is True


@pytest.mark.asyncio
async def test_execution_cleanup_resumes_from_persisted_open_journal(tmp_path: Path) -> None:
    journal_path = tmp_path / "execution.json"
    client = FakeExecutionClient(journal_path)
    persisted = Journal.begin_submission(
        path=journal_path,
        run_id="run-1",
        account_fingerprint="account-abcd",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(JournalState.OPEN, order_ref="deal-42")
    client.position_open = True
    restarted_manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
    )

    cleanup_ref = await restarted_manager.cleanup(
        journal=Journal.load(journal_path),
        execute=True,
        client_order_id=124,
    )

    assert persisted.state is JournalState.OPEN
    assert cleanup_ref == "liq-43"
    assert client.position_open is False
    assert not journal_path.exists()
