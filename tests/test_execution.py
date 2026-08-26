from pathlib import Path
from typing import Any

import pytest

from trader_api_examples.execution import ExecutionManager
from trader_api_examples.safety import Journal, JournalState


class FakeExecutionClient:
    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path
        self.position_open = False

    async def add_market_deal(self, **_: Any) -> str:
        assert Journal.load(self.journal_path).state is JournalState.PENDING_SUBMISSION
        self.position_open = True
        return "deal-42"

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADER_API_ENABLE_LIVE_TRADING", "true")
    journal_path = tmp_path / "execution.json"
    client = FakeExecutionClient(journal_path)
    manager = ExecutionManager(client=client, journal_path=journal_path, poll_seconds=0)

    journal = await manager.open_position(
        execute=True,
        account_fingerprint="account-abcd",
        contract="EURUSD",
        amount=1000,
        buy=True,
        client_order_id=123,
    )
    assert journal.state is JournalState.OPEN
    assert journal.order_ref == "deal-42"

    cleanup_ref = await manager.cleanup(journal=journal, execute=True, client_order_id=124)

    assert cleanup_ref == "liq-43"
    assert not journal_path.exists()
