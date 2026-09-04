import json
from datetime import UTC, datetime
from typing import Any

import pytest

from trader_api_examples.algo import SignalEvent, StrategyEvaluation
from trader_api_examples.api import ApiError, Bar
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


class PartiallyStaleTraderClient(FakeTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.contract_calls: dict[str, int] = {}

    async def get_completed_bars(self, **kwargs: Any) -> list[Bar]:
        contract = str(kwargs["contract"])
        self.contract_calls[contract] = self.contract_calls.get(contract, 0) + 1
        age_seconds = 600 if contract == "GBPUSD" else 60
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - age_seconds * 1000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40


class RecoveringTraderClient(FakeTraderClient):
    async def get_completed_bars(self, **_: Any) -> list[Bar]:
        self.bar_calls += 1
        age_seconds = 600 if self.bar_calls == 1 else 60
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - age_seconds * 1000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40


class StaleAfterOpenTraderClient(ExecutingTraderClient):
    async def get_completed_bars(self, **_: Any) -> list[Bar]:
        self.bar_calls += 1
        age_seconds = 60 if self.bar_calls == 1 else 600
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - age_seconds * 1000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40


class PartiallyRejectedTraderClient(FakeTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.contract_calls: dict[str, int] = {}

    async def get_completed_bars(self, **kwargs: Any) -> list[Bar]:
        contract = str(kwargs["contract"])
        self.contract_calls[contract] = self.contract_calls.get(contract, 0) + 1
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - 60_000
        close = 2.0 if contract == "GBPUSD" else 1.0
        return [Bar(time_ms=time_ms, open=close, high=close, low=close, close=close)] * 40

    async def add_market_deal(self, **_: Any) -> str:
        raise ApiError("addDeal returned HTTP 400 (Not Available to trade this contract).", 400)


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
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD", amount=1000, max_runtime_seconds=0.1, poll_seconds=0.001
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
                    max_runtime_seconds=0.1,
                    poll_seconds=0.001,
                ),
                StrategyConfig(),
            ),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    FakePriceSession.enter_count = 0
    FakePriceSession.contracts = []
    monkeypatch.chdir(tmp_path)
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
async def test_stale_bars_pause_only_the_affected_instance(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.05,
        poll_seconds=0.001,
        market_data_retry_seconds=0.001,
        stale_position_grace_seconds=0.02,
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
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
            AlgoInstanceConfig(
                "stale",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.05,
                    poll_seconds=0.001,
                    market_data_retry_seconds=0.001,
                    stale_position_grace_seconds=0.02,
                ),
                StrategyConfig(),
            ),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    client = PartiallyStaleTraderClient()
    FakePriceSession.contracts = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.outcome.value == "INCONCLUSIVE"
    assert result.data["instances"]["healthy"]["status"] == "runtime-limit"
    assert result.data["instances"]["stale"]["status"] == "stale-bars"
    assert client.contract_calls["EURUSD"] > 1
    assert client.contract_calls["GBPUSD"] > 1


@pytest.mark.asyncio
async def test_paused_instance_recovers_when_chart_bars_resume(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.05,
        poll_seconds=0.001,
        market_data_retry_seconds=0.001,
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
    )
    client = RecoveringTraderClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.outcome.value == "NO_SIGNAL"
    assert result.data["failed_instances"] == []
    assert result.data["instances"]["default"]["status"] == "runtime-limit"
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "MARKET_DATA_PAUSED" in events
    assert "MARKET_DATA_RECOVERED" in events


@pytest.mark.asyncio
async def test_stale_bars_liquidate_owned_position_only_after_grace_period(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.2,
        poll_seconds=0.001,
        market_data_retry_seconds=0.001,
        stale_position_grace_seconds=0.005,
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
    client = StaleAfterOpenTraderClient()

    def open_signal(*_args: Any, **_kwargs: Any) -> StrategyEvaluation:
        return StrategyEvaluation(SignalEvent(1, Signal.OPEN_BUY, 50, 1, "rsi"), {"rsi": 50})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", open_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "INCONCLUSIVE"
    assert client.add_count == 1
    assert client.liquidate_count == 1
    assert not (tmp_path / "runtime/execution-default-eurusd.json").exists()


@pytest.mark.asyncio
async def test_algo_runner_executes_rest_round_trip_and_clears_journal(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    def next_signal(*_args: Any, **_kwargs: Any) -> StrategyEvaluation:
        signal = next(signals)
        return StrategyEvaluation(SignalEvent(1, signal, 50, 1, "rsi"), {"rsi": 50})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", next_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.add_count == 1
    assert client.liquidate_count == 1
    assert not (tmp_path / "runtime/execution-default-eurusd.json").exists()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events == [
        "STARTED",
        "PRICE_SESSION_CONNECTED",
        "HISTORY_LOADED",
        "BAR_EVALUATED",
        "SIGNAL",
        "ORDER_SUBMITTED",
        "POSITION_CONFIRMED",
        "BAR_EVALUATED",
        "SIGNAL",
        "LIQUIDATE_SUBMITTED",
        "POSITION_CLOSED",
        "STOPPED",
    ]
    stderr = capsys.readouterr().err
    assert "[Runner] Starting mode=live-execute" in stderr
    assert "[REST:default] Contract settings validated" in stderr
    assert "[Price] Shared price session connected" in stderr
    assert "[Price:default] EURUSD bid=1.0 ask=1.1 tag=yes" in stderr
    assert "[Chart:default] Historical candles loaded count=40 period_type=1" in stderr
    assert "[Strategy:default] Indicators calculated" in stderr
    assert "indicators=rsi=50" in stderr
    assert "[REST:default] Position confirmed deal_ref=deal-1" in stderr
    assert "[REST:default] Position closed cleanup_ref=liquidate-1" in stderr


@pytest.mark.asyncio
async def test_trade_rejection_disables_only_the_affected_instance(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD", amount=1000, max_runtime_seconds=0.1, poll_seconds=0.001
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
            AlgoInstanceConfig(
                "rejected",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.1,
                    poll_seconds=0.001,
                ),
                StrategyConfig(),
            ),
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = PartiallyRejectedTraderClient()

    def reject_one_contract(bars: list[Bar], **_: Any) -> StrategyEvaluation:
        event = (
            SignalEvent(1, Signal.OPEN_SELL, 50, bars[-1].close, "rsi")
            if bars[-1].close == 2
            else None
        )
        return StrategyEvaluation(event, {"rsi": 50})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())
    monkeypatch.setattr(
        "trader_api_examples.commands.evaluate_latest_strategy", reject_one_contract
    )

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "INCONCLUSIVE"
    assert result.data["failed_instances"] == ["rejected"]
    assert result.data["instances"]["rejected"]["status"] == "trade-rejected"
    assert result.data["instances"]["healthy"]["status"] == "runtime-limit"
    assert client.contract_calls["EURUSD"] > 1
    assert not (tmp_path / "runtime/execution-rejected-gbpusd.json").exists()
