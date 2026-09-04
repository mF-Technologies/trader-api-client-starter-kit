from pathlib import Path
from typing import Any

import pytest

from trader_api_examples.api import ApiError
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


class RetryCleanupClient(FakeExecutionClient):
    def __init__(self, journal_path: Path) -> None:
        super().__init__(journal_path)
        self.cleanup_ids: list[int] = []

    async def liquidate_market_deal(self, **kwargs: Any) -> str:
        self.cleanup_ids.append(int(kwargs["client_order_id"]))
        if len(self.cleanup_ids) > 1:
            self.position_open = False
        return "liq-43"


class RejectedExecutionClient(FakeExecutionClient):
    async def add_market_deal(self, **_: Any) -> str:
        raise ApiError("addDeal returned HTTP 400 (Not Available to trade this contract).", 400)


class TransientCleanupClient(FakeExecutionClient):
    def __init__(self, journal_path: Path, error_code: str) -> None:
        super().__init__(journal_path)
        self.error_code = error_code
        self.cleanup_ids: list[int] = []

    async def liquidate_market_deal(self, **kwargs: Any) -> str:
        self.cleanup_ids.append(int(kwargs["client_order_id"]))
        if len(self.cleanup_ids) == 1:
            raise ApiError(
                f"liquidate returned HTTP 400 ({self.error_code}).",
                400,
                self.error_code,
            )
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
    )
    assert journal.state is JournalState.OPEN
    assert journal.order_ref == "deal-42"

    cleanup_ref = await manager.cleanup(journal=journal, execute=True, client_order_id=124)

    assert cleanup_ref == "liq-43"
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_cleanup_retry_reuses_the_persisted_client_order_id(tmp_path: Path) -> None:
    journal_path = tmp_path / "execution.json"
    client = RetryCleanupClient(journal_path)
    manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
        confirmation_attempts=1,
    )
    journal = await manager.open_position(
        execute=True,
        account_fingerprint="account-abcd",
        contract="EURUSD",
        amount=1000,
        buy=True,
        client_order_id=123,
    )

    with pytest.raises(RuntimeError, match="zero position was not confirmed"):
        await manager.cleanup(journal=journal, execute=True, client_order_id=124)

    cleanup_ref = await manager.cleanup(journal=journal, execute=True, client_order_id=999)

    assert cleanup_ref == "liq-43"
    assert client.cleanup_ids == [124, 124]
    assert not journal_path.exists()


@pytest.mark.asyncio
async def test_definitive_submission_rejection_clears_execution_journal(tmp_path: Path) -> None:
    journal_path = tmp_path / "execution.json"
    manager = ExecutionManager(
        client=RejectedExecutionClient(journal_path),
        journal_path=journal_path,
        live_trading_enabled=True,
    )

    with pytest.raises(ApiError, match="Not Available"):
        await manager.open_position(
            execute=True,
            account_fingerprint="account-abcd",
            contract="LLS",
            amount=1000,
            buy=False,
            client_order_id=123,
        )

    assert not journal_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["710", "934"])
async def test_cleanup_retries_transient_market_rejection_with_new_persisted_id(
    tmp_path: Path, error_code: str
) -> None:
    journal_path = tmp_path / "execution.json"
    client = TransientCleanupClient(journal_path, error_code)
    manager = ExecutionManager(
        client=client,
        journal_path=journal_path,
        live_trading_enabled=True,
        poll_seconds=0,
        client_order_id_factory=lambda: 789,
    )
    client.position_open = True
    journal = Journal.begin_submission(
        path=journal_path,
        run_id="run-1",
        account_fingerprint="account-abcd",
        contract="LLG",
        side="BUY",
        amount=10,
        client_order_id=123,
    ).with_state(JournalState.OPEN, order_ref="deal-42")

    cleanup_ref = await manager.cleanup(journal=journal, execute=True, client_order_id=456)

    assert cleanup_ref == "liq-43"
    assert client.cleanup_ids == [456, 789]
    assert not journal_path.exists()
