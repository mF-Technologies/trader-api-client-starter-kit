from datetime import UTC, datetime
from typing import Any

import pytest

from trader_api_examples.algo import SignalEvent
from trader_api_examples.api import Bar
from trader_api_examples.commands import algo_runner
from trader_api_examples.config import (
    AlgoInstanceConfig,
    AppConfig,
    EndpointsConfig,
    Secrets,
    StrategyConfig,
    TradingConfig,
)
from trader_api_examples.price_client import Quote
from trader_api_examples.strategy import Signal


class FakeTraderClient:
    def __init__(self) -> None:
        self.bar_calls = 0

    async def __aenter__(self) -> "FakeTraderClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_contract_settings(self) -> list[dict[str, Any]]:
        return [
            {
                "market": contract,
                "contractSize": 100_000,
                "minTradeLot": 0.01,
                "minLotIncrementUnit": 0.01,
            }
            for contract in ("EURUSD", "GBPUSD")
        ]

    async def get_completed_bars(self, **_: Any) -> list[Bar]:
        self.bar_calls += 1
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - 120_000 + self.bar_calls * 60_000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40

    async def add_market_deal(self, **_: Any) -> str:
        raise AssertionError("observe mode must not submit a deal")


class ExecutingTraderClient(FakeTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.add_count = 0
        self.liquidate_count = 0
        self.position_open = False

    async def add_market_deal(self, **_: Any) -> str:
        self.add_count += 1
        self.position_open = True
        return "deal-1"

    async def get_position_detail(self, order_ref: str) -> dict[str, str] | None:
        return {"dealRef": order_ref} if self.position_open else None

    async def liquidate_market_deal(self, **_: Any) -> str:
        self.liquidate_count += 1
        self.position_open = False
        return "liquidate-1"


class FakePriceSession:
    enter_count = 0
    contracts: list[str] = []

    def __init__(self, _: AppConfig) -> None:
        pass

    async def __aenter__(self) -> "FakePriceSession":
        type(self).enter_count += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_quote(self, contract: str, **_: Any) -> Quote:
        type(self).contracts.append(contract)
        return Quote(contract, 1.0, 1.1, f"{contract}-tag")


class FakeHeartbeat:
    def write(self, _: dict[str, str]) -> None:
        pass


@pytest.mark.asyncio
async def test_algo_runner_shares_price_session_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD", amount=1000, max_runtime_seconds=0.01, poll_seconds=0.001
    )
    config = AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://webproxy.example",
            fxserver_rest_url="https://fxserver.example",
            chart_server_url="https://chart.example",
        ),
        trading=trading,
        strategy=StrategyConfig(),
        instances=(
            AlgoInstanceConfig("euro", trading, StrategyConfig()),
            AlgoInstanceConfig(
                "sterling",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.01,
                    poll_seconds=0.001,
                ),
                StrategyConfig(),
            ),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    FakePriceSession.enter_count = 0
    FakePriceSession.contracts = []
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: FakeTraderClient()
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.data["shared_price_session"] is True
    assert FakePriceSession.enter_count == 1
    assert set(FakePriceSession.contracts) == {"EURUSD", "GBPUSD"}


@pytest.mark.asyncio
async def test_algo_runner_executes_rest_round_trip_and_clears_journal(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    trading = TradingConfig(
        contract="EURUSD", amount=1000, max_runtime_seconds=1, poll_seconds=0.001
    )
    config = AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://webproxy.example",
            fxserver_rest_url="https://fxserver.example",
            chart_server_url="https://chart.example",
        ),
        trading=trading,
        strategy=StrategyConfig(),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = ExecutingTraderClient()
    signals = iter((Signal.OPEN_BUY, Signal.CLOSE_BUY))

    def next_signal(*_args: Any, **_kwargs: Any) -> SignalEvent:
        signal = next(signals)
        return SignalEvent(1, signal, 50, 1, "rsi")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.latest_strategy_signal", next_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.add_count == 1
    assert client.liquidate_count == 1
    assert not (tmp_path / "runtime/execution-default-eurusd.json").exists()
