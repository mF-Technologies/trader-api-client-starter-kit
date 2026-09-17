from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader_api_examples import commands
from trader_api_examples.algo import Outcome
from trader_api_examples.api import Bar
from trader_api_examples.config import (
    AppConfig,
    EndpointsConfig,
    Secrets,
    StrategyConfig,
    TradingConfig,
)
from trader_api_examples.ema_algo import EmaSignalEvent
from trader_api_examples.price_client import Quote
from trader_api_examples.safety import Journal, JournalState, LiveExecutionBlocked, LiveRiskState
from trader_api_examples.sma_algo import SmaSignalEvent
from trader_api_examples.strategy import Signal


def bar_at(timestamp: datetime, price: float = 100.0) -> Bar:
    time_ms = int(timestamp.timestamp() * 1000)
    return Bar(time_ms=time_ms, open=price, high=price + 0.1, low=price - 0.1, close=price)


def test_market_data_health_allows_observed_daily_break() -> None:
    previous = datetime(2026, 1, 5, 23, tzinfo=UTC)
    current = datetime(2026, 1, 6, 1, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(previous), bar_at(current)],
        2,
        now=(current + timedelta(minutes=30)).timestamp(),
    )

    assert health.unusable is False
    assert health.issue is None


def test_market_data_health_allows_weekend_gap() -> None:
    previous = datetime(2026, 1, 9, 23, tzinfo=UTC)
    current = datetime(2026, 1, 12, 1, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(previous), bar_at(current)],
        2,
        now=(current + timedelta(minutes=30)).timestamp(),
    )

    assert health.unusable is False


def test_market_data_health_does_not_mark_weekend_latest_bar_stale() -> None:
    latest = datetime(2026, 1, 9, 23, tzinfo=UTC)
    current = datetime(2026, 1, 10, 12, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(latest)],
        2,
        now=current.timestamp(),
    )

    assert health.stale_latest_bar is False
    assert health.unusable is False


def test_market_data_health_allows_first_forming_bar_after_daily_break() -> None:
    latest = datetime(2026, 1, 5, 22, tzinfo=UTC)
    current = datetime(2026, 1, 6, 1, 30, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(latest)],
        2,
        now=current.timestamp(),
    )

    assert health.stale_latest_bar is False
    assert health.unusable is False


@pytest.mark.parametrize("missing_bar_count", [1, 2, 4])
def test_market_data_health_detects_internal_h1_gap(missing_bar_count: int) -> None:
    previous = datetime(2026, 1, 5, 10, tzinfo=UTC)
    current = previous + timedelta(hours=missing_bar_count + 1)

    health = commands._market_data_health(
        [bar_at(previous), bar_at(current)],
        2,
        now=(current + timedelta(minutes=30)).timestamp(),
    )

    assert health.unusable is True
    assert health.stale_latest_bar is False
    assert health.unexpected_internal_gap is True
    assert health.missing_bar_count == missing_bar_count


def test_market_data_health_detects_stale_latest_bar() -> None:
    latest = datetime(2026, 1, 5, 10, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(latest)],
        2,
        now=(latest + timedelta(hours=3)).timestamp(),
    )

    assert health.unusable is True
    assert health.stale_latest_bar is True
    assert health.issue == "STALE_OR_MISSING_LATEST_BAR"


def test_market_data_health_allows_configured_one_bar_publication_lag() -> None:
    latest = datetime(2026, 1, 5, 10, tzinfo=UTC)

    health = commands._market_data_health(
        [bar_at(latest)],
        1,
        now=(latest + timedelta(minutes=2, seconds=30)).timestamp(),
        max_age_intervals=3,
    )

    assert health.stale_latest_bar is False
    assert health.unusable is False


class FakeMarketDataClient:
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars

    async def __aenter__(self) -> "FakeMarketDataClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_contract_settings(self) -> list[dict[str, object]]:
        return [
            {
                "market": "LLG",
                "contractSize": 1,
                "minTradeLot": 1,
                "minLotIncrementUnit": 1,
            }
        ]

    async def get_completed_bars(self, **_: object) -> list[Bar]:
        return self.bars

    async def get_account_balance(self) -> dict[str, object]:
        return {"equity": 1000.0, "currency": "USD"}


def live_sma_config(*, max_total_open_positions: int = 1) -> AppConfig:
    return AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://web.example",
            fxserver_rest_url="https://fx.example",
            chart_server_url="https://chart.example",
        ),
        trading=TradingConfig(
            contract="LLG",
            amount=10,
            max_total_open_positions=max_total_open_positions,
            max_runtime_seconds=1,
            max_holding_hours=1,
            poll_seconds=0,
            bar_count=200,
        ),
        strategy=StrategyConfig(sma_period_type=2),
        secrets=Secrets(api_key="test-api-key"),
        live_trading_enabled=True,
    )


@pytest.mark.asyncio
async def test_live_ema_rejects_order_execution_before_connecting() -> None:
    with pytest.raises(LiveExecutionBlocked, match="EMA live execution is intentionally disabled"):
        await commands.live_ema_algo(live_sma_config(), "live-execute", execute=True)


@pytest.mark.asyncio
async def test_live_ema_observe_reports_signal_without_order_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    fake_client = FakeMarketDataClient([bar_at(now - timedelta(minutes=30))])
    monkeypatch.setattr(commands.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)

    def bullish_signal(*_args: object, **_kwargs: object) -> EmaSignalEvent:
        return EmaSignalEvent(
            signal_time_ms=fake_client.bars[-1].time_ms,
            execution_time_ms=None,
            signal=Signal.OPEN_BUY,
            reason="BULLISH_EMA_CROSSOVER",
            ema_fast=101.0,
            ema_slow=100.0,
            atr=1.0,
            close=100.0,
        )

    monkeypatch.setattr(commands, "latest_ema_signal", bullish_signal)

    result = await commands.live_ema_algo(live_sma_config(), "live-observe", execute=False)

    assert result.outcome is Outcome.SUCCESS
    assert result.data["period_type"] == 2
    assert result.data["proposed_event"]["reason"] == "BULLISH_EMA_CROSSOVER"


@pytest.mark.asyncio
async def test_live_sma_blocks_internal_gap_without_submitting_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    current = now - timedelta(minutes=30)
    previous = current - timedelta(hours=2)
    monkeypatch.setattr(commands.time, "time", lambda: now.timestamp())
    fake_client = FakeMarketDataClient([bar_at(previous), bar_at(current)])
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)

    result = await commands.live_sma_algo(live_sma_config(), "live-observe", execute=False)

    assert result.outcome is Outcome.INCONCLUSIVE
    assert "unexpected internal gap" in result.message
    assert result.data["missing_bar_count"] == 1
    assert result.data["market_data_issue"] == "UNEXPECTED_INTERNAL_GAP"


@pytest.mark.asyncio
async def test_live_sma_runtime_limit_applies_while_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLoop:
        def __init__(self) -> None:
            self.calls = 0

        def time(self) -> float:
            self.calls += 1
            return float(self.calls - 1)

    fake_loop = FakeLoop()
    monkeypatch.setattr(commands.asyncio, "get_running_loop", lambda: fake_loop)
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: FakeMarketDataClient([]))

    result = await commands.live_sma_algo(live_sma_config(), "live-observe", execute=False)

    assert result.outcome is Outcome.NO_SIGNAL
    assert result.message == "No SMA crossover occurred before the flat runtime limit."
    assert result.data["max_runtime_seconds"] == 1


@pytest.mark.asyncio
async def test_live_sma_blocks_unrelated_open_position(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OccupiedClient(FakeMarketDataClient):
        async def get_positions(self) -> list[dict[str, object]]:
            return [{"ref": "manual-deal", "contract": "LLG", "amount": 10}]

    fake_client = OccupiedClient([])
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)
    monkeypatch.setattr(commands, "_journal_path", lambda _config: tmp_path / "execution.json")

    result = await commands.live_sma_algo(live_sma_config(), "live-execute", execute=True)

    assert result.outcome is Outcome.BLOCKED
    assert result.data["unrelated_position_count"] == 1
    assert result.data["open_position_count"] == 1
    assert result.data["max_managed_positions"] == 1
    assert result.data["max_total_open_positions"] == 1


@pytest.mark.asyncio
async def test_live_sma_allows_seven_unrelated_positions_with_eighth_position_capacity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OccupiedClient(FakeMarketDataClient):
        async def get_positions(self) -> list[dict[str, object]]:
            return [
                {"ref": f"manual-deal-{index}", "contract": "LLG", "amount": 10}
                for index in range(7)
            ]

    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    fake_client = OccupiedClient([bar_at(now - timedelta(minutes=30))])
    monkeypatch.setattr(commands.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)

    def bullish_signal(*_args: object, **_kwargs: object) -> SmaSignalEvent:
        return SmaSignalEvent(
            signal_time_ms=fake_client.bars[-1].time_ms,
            execution_time_ms=None,
            signal=Signal.OPEN_BUY,
            reason="BULLISH_SMA_CROSSOVER",
            sma_fast=101.0,
            sma_slow=100.0,
            atr=1.0,
            close=100.0,
        )

    monkeypatch.setattr(commands, "latest_sma_signal", bullish_signal)

    async def read_safe_quote(*_args: object, **_kwargs: object) -> Quote:
        return Quote(contract="LLG", bid=100.0, ask=100.1, tag="test")

    monkeypatch.setattr(
        commands, "read_quote", read_safe_quote
    )

    class StopAfterOpen(Exception):
        pass

    class FakeExecutionManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def open_position(self, **_kwargs: object) -> None:
            raise StopAfterOpen

    monkeypatch.setattr(commands, "ExecutionManager", FakeExecutionManager)
    monkeypatch.setattr(commands, "_risk_state_path", lambda _config: tmp_path / "risk.json")
    monkeypatch.setattr(commands, "_journal_path", lambda _config: tmp_path / "execution.json")

    with pytest.raises(StopAfterOpen):
        await commands.live_sma_algo(
            live_sma_config(max_total_open_positions=8), "live-execute", execute=True
        )


@pytest.mark.asyncio
async def test_live_sma_blocks_when_total_position_capacity_is_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OccupiedClient(FakeMarketDataClient):
        async def get_positions(self) -> list[dict[str, object]]:
            return [
                {"ref": f"manual-deal-{index}", "contract": "LLG", "amount": 10}
                for index in range(8)
            ]

    fake_client = OccupiedClient([])
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)
    monkeypatch.setattr(commands, "_journal_path", lambda _config: tmp_path / "execution.json")

    result = await commands.live_sma_algo(
        live_sma_config(max_total_open_positions=8), "live-execute", execute=True
    )

    assert result.outcome is Outcome.BLOCKED
    assert result.data["open_position_count"] == 8
    assert result.data["max_managed_positions"] == 1
    assert result.data["max_total_open_positions"] == 8


@pytest.mark.asyncio
async def test_live_sma_open_position_uses_holding_limit_after_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeLiveClient(FakeMarketDataClient):
        def __init__(self, bars: list[Bar]) -> None:
            super().__init__(bars)
            self.position_open = True

        async def get_positions(self) -> list[dict[str, object]]:
            return (
                [{"ref": "deal-42", "contract": "LLG", "amount": 10}]
                if self.position_open
                else []
            )

        async def get_position_detail(self, order_ref: str) -> dict[str, object] | None:
            return {"ref": order_ref} if self.position_open else None

        async def liquidate_market_deal(self, **_: object) -> str:
            self.position_open = False
            return "liq-43"

    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    fake_client = FakeLiveClient([bar_at(now - timedelta(minutes=30))])
    journal_path = tmp_path / "execution-llg.json"
    Journal.begin_submission(
        path=journal_path,
        run_id="run-1",
        account_fingerprint=commands.account_fingerprint("test-api-key"),
        contract="LLG",
        side="BUY",
        amount=10,
        client_order_id=123,
        entry_time_ms=int((now - timedelta(hours=2)).timestamp() * 1000),
        stop_price=95.0,
    ).with_state(JournalState.OPEN, order_ref="deal-42")

    fake_loop_time = iter((0.0, 2.0))
    monkeypatch.setattr(commands.asyncio, "get_running_loop", lambda: type(
        "FakeLoop", (), {"time": lambda _self: next(fake_loop_time)}
    )())
    monkeypatch.setattr(commands.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)
    monkeypatch.setattr(commands, "_journal_path", lambda _config: journal_path)
    risk_path = tmp_path / "risk-llg.json"
    monkeypatch.setattr(commands, "_risk_state_path", lambda _config: risk_path)

    async def read_safe_quote(*_args: object, **_kwargs: object) -> Quote:
        return Quote(contract="LLG", bid=100.0, ask=100.1, tag="test")

    monkeypatch.setattr(commands, "read_quote", read_safe_quote)

    result = await commands.live_sma_algo(live_sma_config(), "live-execute", execute=True)

    assert result.outcome is Outcome.SUCCESS
    assert "maximum holding time" in result.message.lower()
    assert fake_client.position_open is False
    assert not journal_path.exists()


def test_live_stop_tightens_atr_stop_to_trade_loss_budget() -> None:
    stop_price = commands._live_stop_price(
        entry_price=100.0,
        atr=10.0,
        atr_stop_multiple=2.0,
        amount=10.0,
        entry_equity=1000.0,
        max_trade_loss_pct=1.0,
    )

    assert stop_price == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_live_sma_daily_loss_closes_position_and_blocks_trading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class LosingLiveClient(FakeMarketDataClient):
        def __init__(self, bars: list[Bar]) -> None:
            super().__init__(bars)
            self.position_open = True

        async def get_account_balance(self) -> dict[str, object]:
            return {"equity": 970.0, "currency": "USD"}

        async def get_positions(self) -> list[dict[str, object]]:
            return [{"ref": "deal-42", "contract": "LLG", "amount": 10}]

        async def get_position_detail(self, order_ref: str) -> dict[str, object] | None:
            return {"ref": order_ref} if self.position_open else None

        async def liquidate_market_deal(self, **_: object) -> str:
            self.position_open = False
            return "liq-43"

    now = datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    fake_client = LosingLiveClient([bar_at(now - timedelta(minutes=30))])
    journal_path = tmp_path / "execution-llg.json"
    risk_path = tmp_path / "risk-llg.json"
    Journal.begin_submission(
        path=journal_path,
        run_id="run-1",
        account_fingerprint=commands.account_fingerprint("test-api-key"),
        contract="LLG",
        side="BUY",
        amount=10,
        client_order_id=123,
        entry_time_ms=int((now - timedelta(hours=2)).timestamp() * 1000),
        stop_price=95.0,
    ).with_state(JournalState.OPEN, order_ref="deal-42")
    LiveRiskState.initialize(
        path=risk_path,
        account_fingerprint=commands.account_fingerprint("test-api-key"),
        contract="LLG",
        utc_date="2026-01-05",
        equity=1000.0,
    )

    monkeypatch.setattr(commands.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(commands, "make_client", lambda *_args, **_kwargs: fake_client)
    monkeypatch.setattr(commands, "_journal_path", lambda _config: journal_path)
    monkeypatch.setattr(commands, "_risk_state_path", lambda _config: risk_path)

    result = await commands.live_sma_algo(live_sma_config(), "live-execute", execute=True)

    assert result.outcome is Outcome.BLOCKED
    assert result.data["risk_limits"] == ["MAX_DAILY_LOSS"]
    assert fake_client.position_open is False
    assert not journal_path.exists()
    assert LiveRiskState.load(risk_path).daily_loss_triggered is True


def test_live_risk_limit_state_can_be_reloaded_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "risk.json"
    initial = LiveRiskState.initialize(
        path=path,
        account_fingerprint="account-1234",
        contract="LLG",
        utc_date="2026-01-05",
        equity=1000.0,
    )
    initial.with_updates(drawdown_triggered=True)

    restarted = LiveRiskState.load(path)

    assert restarted.account_fingerprint == "account-1234"
    assert restarted.drawdown_triggered is True
