import asyncio
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
from trader_api_examples.price_client import Quote, QuoteUnavailableError
from trader_api_examples.runtime import instance_journal_path
from trader_api_examples.safety import Journal, JournalState
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

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def reconcile_market_deal(self, **_: Any) -> str | None:
        return None

    async def add_market_deal(self, **_: Any) -> str:
        raise AssertionError("observe mode must not submit a deal")


class ExecutingTraderClient(FakeTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.add_count = 0
        self.liquidate_count = 0
        self.position_open = False
        self.liquidate_ids: list[int] = []

    async def add_market_deal(self, **_: Any) -> str:
        self.add_count += 1
        self.position_open = True
        return "deal-1"

    async def reconcile_market_deal(self, **_: Any) -> str | None:
        return "deal-1" if self.position_open else None

    async def get_position_detail(self, order_ref: str) -> dict[str, str] | None:
        return {"dealRef": order_ref} if self.position_open else None

    async def liquidate_market_deal(self, **kwargs: Any) -> str:
        self.liquidate_count += 1
        self.liquidate_ids.append(int(kwargs["client_order_id"]))
        self.position_open = False
        return "liquidate-1"


class TransientPositionDetailTraderClient(ExecutingTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.position_detail_calls = 0

    async def get_position_detail(self, order_ref: str) -> dict[str, str] | None:
        self.position_detail_calls += 1
        if self.position_detail_calls == 1:
            raise ApiError("position detail returned HTTP 502.", 502)
        return await super().get_position_detail(order_ref)


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


class NearStaleTraderClient(FakeTraderClient):
    async def get_completed_bars(self, **_: Any) -> list[Bar]:
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - 125_000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40


class RecoveringChartServerClient(FakeTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.contract_calls: dict[str, int] = {}

    async def get_completed_bars(self, **kwargs: Any) -> list[Bar]:
        contract = str(kwargs["contract"])
        attempts = self.contract_calls.get(contract, 0) + 1
        self.contract_calls[contract] = attempts
        if contract == "GBPUSD" and attempts <= 2:
            raise ApiError("/fapi/chartCode returned HTTP 502.", 502)
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - 60_000
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


class TemporarilyIlliquidCleanupClient(ExecutingTraderClient):
    def __init__(self) -> None:
        super().__init__()
        self.position_open = True
        self.contract_calls: dict[str, int] = {}

    async def get_completed_bars(self, **kwargs: Any) -> list[Bar]:
        contract = str(kwargs["contract"])
        self.contract_calls[contract] = self.contract_calls.get(contract, 0) + 1
        time_ms = int(datetime.now(UTC).timestamp() * 1000) - 60_000
        return [Bar(time_ms=time_ms, open=1, high=1, low=1, close=1)] * 40

    async def liquidate_market_deal(self, **kwargs: Any) -> str:
        self.liquidate_count += 1
        self.liquidate_ids.append(int(kwargs["client_order_id"]))
        if self.liquidate_count <= 5:
            raise ApiError("liquidate returned HTTP 400 (934).", 400, "934")
        self.position_open = False
        return "liquidate-1"


class AlwaysIlliquidCleanupClient(TemporarilyIlliquidCleanupClient):
    async def liquidate_market_deal(self, **kwargs: Any) -> str:
        self.liquidate_count += 1
        self.liquidate_ids.append(int(kwargs["client_order_id"]))
        raise ApiError("liquidate returned HTTP 400 (934).", 400, "934")


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


class FailingPriceSession(FakePriceSession):
    async def get_quote(self, contract: str, **_: Any) -> Quote:
        raise TimeoutError(f"Shared price stream failed for {contract}.")


class PartiallyUnavailablePriceSession(FakePriceSession):
    async def get_quote(self, contract: str, **_: Any) -> Quote:
        if contract == "GBPUSD":
            raise QuoteUnavailableError(f"No fresh quote received for {contract}.")
        return await super().get_quote(contract)


class RecoveringQuotePriceSession(FakePriceSession):
    attempts: dict[str, int] = {}

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        type(self).attempts = {}

    async def get_quote(self, contract: str, **_: Any) -> Quote:
        attempts = self.attempts.get(contract, 0) + 1
        self.attempts[contract] = attempts
        if contract == "GBPUSD" and attempts <= 2:
            raise QuoteUnavailableError(f"No fresh quote received for {contract}.")
        return await super().get_quote(contract)


class FakeHeartbeat:
    def write(self, _: dict[str, str]) -> None:
        pass


@pytest.mark.asyncio
async def test_algo_runner_keeps_running_when_runtime_limit_is_omitted(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://webproxy.example",
            fxserver_rest_url="https://fxserver.example",
            chart_server_url="https://chart.example",
        ),
        trading=TradingConfig(
            contract="EURUSD", amount=1000, max_runtime_seconds=None, poll_seconds=0.001
        ),
        strategy=StrategyConfig(),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    client = FakeTraderClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(algo_runner(config, "live-observe", execute=False), timeout=0.03)

    assert client.bar_calls >= 1


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
async def test_m1_bar_within_stale_grace_does_not_pause_instance(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://webproxy.example",
            fxserver_rest_url="https://fxserver.example",
            chart_server_url="https://chart.example",
        ),
        trading=TradingConfig(
            contract="EURUSD",
            amount=1000,
            max_runtime_seconds=0.02,
            poll_seconds=0.001,
            market_data_retry_seconds=0.001,
            bar_stale_grace_seconds=60,
        ),
        strategy=StrategyConfig(),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    client = NearStaleTraderClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.data["instances"]["default"]["status"] == "runtime-limit"
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "MARKET_DATA_PAUSED" not in events


@pytest.mark.asyncio
async def test_paused_instance_recovers_when_chart_bars_resume(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.2,
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
        max_runtime_seconds=1.0,
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
        contract="EURUSD", amount=1000, max_runtime_seconds=0.5, poll_seconds=0.001
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
    signals = iter(
        (
            Signal.OPEN_BUY,
            Signal.CLOSE_BUY,
            Signal.OPEN_SELL,
            Signal.CLOSE_SELL,
        )
    )

    def next_signal(*_args: Any, **_kwargs: Any) -> StrategyEvaluation:
        signal = next(signals, None)
        event = None if signal is None else SignalEvent(1, signal, 50, 1, "rsi")
        return StrategyEvaluation(event, {"rsi": 50})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", next_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.add_count == 2
    assert client.liquidate_count == 2
    assert result.data["instances"]["default"]["status"] == "runtime-limit"
    assert not (tmp_path / "runtime/execution-default-eurusd.json").exists()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    lifecycle_events = [
        event
        for event in events
        if event
        in {
            "STARTED",
            "PRICE_SESSION_CONNECTED",
            "HISTORY_LOADED",
            "SIGNAL",
            "ORDER_SUBMITTED",
            "POSITION_CONFIRMED",
            "LIQUIDATE_SUBMITTED",
            "POSITION_CLOSED",
            "ROUND_TRIP_COMPLETED",
            "STOPPED",
        }
    ]
    assert lifecycle_events == [
        "STARTED",
        "PRICE_SESSION_CONNECTED",
        "HISTORY_LOADED",
        "SIGNAL",
        "ORDER_SUBMITTED",
        "POSITION_CONFIRMED",
        "SIGNAL",
        "LIQUIDATE_SUBMITTED",
        "POSITION_CLOSED",
        "ROUND_TRIP_COMPLETED",
        "SIGNAL",
        "ORDER_SUBMITTED",
        "POSITION_CONFIRMED",
        "SIGNAL",
        "LIQUIDATE_SUBMITTED",
        "POSITION_CLOSED",
        "ROUND_TRIP_COMPLETED",
        "STOPPED",
    ]
    stderr = capsys.readouterr().err
    assert "[Runner] Starting mode=live-execute" in stderr
    assert "[REST:default] Contract settings validated" in stderr
    assert "[Price] Shared price session connected" in stderr
    assert "[Price:default] EURUSD bid=1.0 ask=1.1 tag=yes" in stderr
    assert stderr.count("[Price:default]") == 1
    assert "[Chart:default] Historical candles loaded count=40 period_type=1" in stderr
    assert "[Strategy:default] Indicators calculated" in stderr
    assert "indicators=rsi=50" in stderr
    assert "[REST:default] Position confirmed deal_ref=deal-1" in stderr
    assert "[REST:default] Position closed cleanup_ref=liquidate-1" in stderr
    assert stderr.count("Round trip complete; waiting for the next signal.") == 2
    assert "[Strategy:default] Stopped status=runtime-limit" in stderr


@pytest.mark.asyncio
async def test_algo_runner_restores_open_position_from_journal_after_restart(
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
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = ExecutingTraderClient()
    client.position_open = True
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("default", "EURUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(JournalState.OPEN, order_ref="deal-1")

    def close_signal(*_args: Any, **_kwargs: Any) -> StrategyEvaluation:
        return StrategyEvaluation(SignalEvent(1, Signal.CLOSE_BUY, 50, 1, "rsi"), {"rsi": 50})

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", close_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.add_count == 0
    assert client.liquidate_count == 1
    assert not path.exists()


@pytest.mark.asyncio
async def test_algo_runner_reconciles_unresolved_submission_from_account_state(
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
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = ExecutingTraderClient()
    client.position_open = True
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("default", "EURUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(JournalState.OWNERSHIP_UNCONFIRMED)

    def close_signal(*_args: Any, **_kwargs: Any) -> StrategyEvaluation:
        return StrategyEvaluation(SignalEvent(1, Signal.CLOSE_BUY, 50, 1, "rsi"), {"rsi": 50})

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", close_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.add_count == 0
    assert client.liquidate_count == 1
    assert not path.exists()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "POSITION_RECONCILED" in events


@pytest.mark.asyncio
async def test_algo_runner_retries_transient_position_recovery_before_trading(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.1,
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
        live_trading_enabled=True,
    )
    client = TransientPositionDetailTraderClient()
    client.position_open = True
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("default", "EURUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(JournalState.OPEN, order_ref="deal-1")

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "NO_SIGNAL"
    assert client.position_detail_calls >= 2
    assert client.add_count == 0
    assert client.liquidate_count == 1
    assert not path.exists()
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "runtime/logs/default.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "POSITION_RECOVERY_PENDING" in events
    assert "POSITION_RESTORED" in events
    assert "ERROR" not in events


@pytest.mark.asyncio
async def test_algo_runner_retries_pending_cleanup_before_resuming_after_restart(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD", amount=1000, max_runtime_seconds=0.03, poll_seconds=0.001
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
    client.position_open = True
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("default", "EURUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="EURUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(
        JournalState.CLEANUP_PENDING,
        order_ref="deal-1",
        cleanup_client_order_id=456,
    )

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "NO_SIGNAL"
    assert client.add_count == 0
    assert client.liquidate_ids == [456]
    assert not path.exists()


@pytest.mark.asyncio
async def test_close_signal_cleanup_rejection_does_not_stop_other_instances(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.5,
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
        instances=(
            AlgoInstanceConfig(
                "cleanup",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.05,
                    poll_seconds=0.001,
                    market_data_retry_seconds=0.001,
                ),
                StrategyConfig(),
            ),
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = TemporarilyIlliquidCleanupClient()
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("cleanup", "GBPUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="GBPUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(JournalState.OPEN, order_ref="deal-1")

    def close_signal(*_args: Any, **kwargs: Any) -> StrategyEvaluation:
        if kwargs["position_side"] is None:
            return StrategyEvaluation(None, {"rsi": 50})
        return StrategyEvaluation(SignalEvent(1, Signal.CLOSE_BUY, 50, 1, "rsi"), {"rsi": 50})

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())
    monkeypatch.setattr("trader_api_examples.commands.evaluate_latest_strategy", close_signal)

    result = await algo_runner(config, "live-execute", execute=True)

    assert result.outcome.value == "SUCCESS"
    assert client.liquidate_count == 6
    assert client.contract_calls["EURUSD"] >= 1
    assert result.data["instances"]["healthy"]["status"] == "runtime-limit"
    assert not path.exists()


@pytest.mark.asyncio
async def test_shutdown_cleanup_does_not_mask_shared_transport_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=None,
        poll_seconds=0.001,
        market_data_retry_seconds=30,
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
                "cleanup",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=None,
                    poll_seconds=0.001,
                    market_data_retry_seconds=30,
                ),
                StrategyConfig(),
            ),
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
        live_trading_enabled=True,
    )
    client = AlwaysIlliquidCleanupClient()
    monkeypatch.chdir(tmp_path)
    path = instance_journal_path("cleanup", "GBPUSD")
    Journal.begin_submission(
        path=path,
        run_id="previous-run",
        account_fingerprint="key-8c284055dbb5",
        contract="GBPUSD",
        side="BUY",
        amount=1000,
        client_order_id=123,
    ).with_state(
        JournalState.CLEANUP_PENDING,
        order_ref="deal-1",
        cleanup_client_order_id=456,
    )

    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FailingPriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    with pytest.raises(TimeoutError, match="Shared price stream failed"):
        await algo_runner(config, "live-execute", execute=True)

    assert path.exists()


@pytest.mark.asyncio
async def test_missing_quote_pauses_only_the_affected_instance(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="EURUSD",
        amount=1000,
        max_runtime_seconds=0.03,
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
        instances=(
            AlgoInstanceConfig(
                "missing",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.03,
                    poll_seconds=0.001,
                    market_data_retry_seconds=0.001,
                ),
                StrategyConfig(),
            ),
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    client = TemporarilyIlliquidCleanupClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr(
        "trader_api_examples.commands.PriceStreamSession", PartiallyUnavailablePriceSession
    )
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.outcome.value == "INCONCLUSIVE"
    assert client.contract_calls["EURUSD"] >= 1
    assert result.data["instances"]["missing"]["failure_reason"] == "stale-quote"
    assert result.data["instances"]["healthy"]["status"] == "runtime-limit"


@pytest.mark.asyncio
async def test_instance_resumes_after_quote_stream_recovers(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading = TradingConfig(
        contract="GBPUSD",
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
    client = TemporarilyIlliquidCleanupClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr(
        "trader_api_examples.commands.PriceStreamSession", RecoveringQuotePriceSession
    )
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.outcome.value == "NO_SIGNAL"
    assert RecoveringQuotePriceSession.attempts["GBPUSD"] >= 3
    assert result.data["instances"]["default"]["failure_reason"] is None


@pytest.mark.asyncio
async def test_transient_chart_error_pauses_only_the_affected_instance(
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
        instances=(
            AlgoInstanceConfig(
                "recovering",
                TradingConfig(
                    contract="GBPUSD",
                    amount=1000,
                    max_runtime_seconds=0.05,
                    poll_seconds=0.001,
                    market_data_retry_seconds=0.001,
                ),
                StrategyConfig(),
            ),
            AlgoInstanceConfig("healthy", trading, StrategyConfig()),
        ),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )
    client = RecoveringChartServerClient()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "trader_api_examples.commands.make_client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr("trader_api_examples.commands.PriceStreamSession", FakePriceSession)
    monkeypatch.setattr("trader_api_examples.commands.HeartbeatWriter", lambda: FakeHeartbeat())

    result = await algo_runner(config, "live-observe", execute=False)

    assert result.outcome.value == "NO_SIGNAL"
    assert client.contract_calls["GBPUSD"] >= 3
    assert client.contract_calls["EURUSD"] >= 1
    assert result.data["instances"]["recovering"]["failure_reason"] is None


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
