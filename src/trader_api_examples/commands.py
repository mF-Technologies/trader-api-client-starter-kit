from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from .algo import AlgoResult, Outcome, evaluate_replay, latest_signal
from .api import PERIOD_DURATION, Bar, TraderApiClient
from .config import AppConfig
from .contracts import calculate_amount, find_contract_setting, validate_amount
from .execution import ExecutionManager
from .output import CommandResult
from .price_client import read_quote
from .safety import (
    Journal,
    JournalState,
    LiveExecutionBlocked,
    assert_live_execution_enabled,
)
from .strategy import PositionSide, Signal


def account_fingerprint(api_key: str) -> str:
    return f"key-{hashlib.sha256(api_key.encode()).hexdigest()[:12]}"


def make_client(config: AppConfig, *, require_chart: bool = False) -> TraderApiClient:
    required = {
        "endpoints.web_proxy_url": config.endpoints.web_proxy_url,
        "endpoints.fxserver_rest_url": config.endpoints.fxserver_rest_url,
    }
    if require_chart:
        required["endpoints.chart_server_url"] = config.endpoints.chart_server_url
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Required endpoint settings are missing: " + ", ".join(missing))
    return TraderApiClient(
        web_proxy_url=config.endpoints.web_proxy_url,
        fxserver_url=config.endpoints.fxserver_rest_url,
        chart_server_url=config.endpoints.chart_server_url,
        api_key=config.secrets.api_key,
    )


def _journal_path(config: AppConfig) -> Path:
    name = "".join(
        character.lower() if character.isalnum() else "-" for character in config.trading.contract
    )
    return Path("runtime") / f"execution-{name}.json"


def _client_order_id() -> int:
    return secrets.randbelow(2_147_483_646) + 1


def _write_execution_summary(config: AppConfig, fingerprint: str, side: str) -> None:
    print(
        "Execution summary: "
        f"account={fingerprint} environment={config.environment} "
        f"contract={config.trading.contract} side={side} amount={config.trading.amount}",
        file=sys.stderr,
    )


def _event_data(result: AlgoResult) -> list[dict[str, Any]]:
    return [
        {
            "time_ms": event.time_ms,
            "signal": event.signal.value,
            "rsi": event.rsi,
            "close": event.close,
        }
        for event in result.events
    ]


async def account_inspector(config: AppConfig) -> CommandResult:
    async with make_client(config) as client:
        balance, positions, orders = await asyncio.gather(
            client.get_account_balance(), client.get_positions(), client.get_orders()
        )
    return CommandResult(
        "account-inspector",
        Outcome.SUCCESS,
        "Account state retrieved.",
        {
            "account_fingerprint": account_fingerprint(config.secrets.api_key),
            "environment": config.environment,
            "balance": {
                key: value
                for key, value in balance.items()
                if key.lower() in {"available", "balance", "currency", "equity", "margin"}
            },
            "open_positions": len(positions),
            "working_orders": len(orders["workingOrders"]),
        },
    )


async def contract_calculator(config: AppConfig, lots: float) -> CommandResult:
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
    calculation = calculate_amount(lots=lots, contract_setting=setting)
    return CommandResult(
        "contract-calculator",
        Outcome.SUCCESS,
        "Contract amount calculated.",
        {
            "contract": config.trading.contract,
            "lots": calculation.lots,
            "amount": calculation.amount,
            "minimum_amount": calculation.minimum_amount,
            "increment_amount": calculation.increment_amount,
        },
    )


async def market_data_monitor(config: AppConfig, with_quote: bool) -> CommandResult:
    async with make_client(config, require_chart=True) as client:
        bars = await client.get_completed_bars(
            contract=config.trading.contract,
            period_type=config.strategy.period_type,
            count=config.trading.bar_count,
        )
    data: dict[str, Any] = {
        "contract": config.trading.contract,
        "completed_bar_count": len(bars),
        "latest_completed_bar": vars(bars[-1]) if bars else None,
    }
    if with_quote:
        quote = await read_quote(config)
        data["quote"] = {
            "contract": quote.contract,
            "bid": quote.bid,
            "ask": quote.ask,
            "tag_present": bool(quote.tag),
        }
    outcome = Outcome.SUCCESS if bars else Outcome.INCONCLUSIVE
    message = "Market data retrieved." if bars else "No completed historical bars were returned."
    return CommandResult("market-data-monitor", outcome, message, data)


def load_replay_fixture(path: Path | None) -> list[Bar]:
    fixture_path = path or Path(
        str(files("trader_api_examples") / "fixtures" / "rsi_reversal.json")
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Replay fixture must be a JSON array.")
    return [
        Bar(
            time_ms=int(item["time"]),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in payload
        if isinstance(item, dict)
    ]


def replay_algo(config: AppConfig, fixture: Path | None) -> CommandResult:
    result = evaluate_replay(
        load_replay_fixture(fixture),
        rsi_period=config.strategy.rsi_period,
        oversold=config.strategy.oversold,
        overbought=config.strategy.overbought,
        exit_level=config.strategy.exit_level,
    )
    return CommandResult(
        "rsi-algo-demo",
        result.outcome,
        result.message,
        {"mode": "replay", "contract": config.trading.contract, "events": _event_data(result)},
    )


async def order_lifecycle(config: AppConfig, execute: bool) -> CommandResult:
    assert_live_execution_enabled(execute=execute)
    fingerprint = account_fingerprint(config.secrets.api_key)
    journal_path = _journal_path(config)
    journal: Journal | None = None
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        manager = ExecutionManager(client=client, journal_path=journal_path)
        _write_execution_summary(config, fingerprint, "BUY")
        try:
            journal = await manager.open_position(
                execute=execute,
                account_fingerprint=fingerprint,
                contract=config.trading.contract,
                amount=config.trading.amount,
                buy=True,
                client_order_id=_client_order_id(),
            )
            cleanup_ref = await manager.cleanup(
                journal=journal,
                execute=execute,
                client_order_id=_client_order_id(),
            )
            journal = None
        finally:
            pending: Journal | None = Journal.load(journal_path) if journal_path.exists() else None
            if pending and pending.order_ref:
                await manager.cleanup(
                    journal=pending,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
    return CommandResult(
        "order-lifecycle-checker",
        Outcome.SUCCESS,
        "Market order was reconciled and cleaned up.",
        {
            "account_fingerprint": fingerprint,
            "environment": config.environment,
            "contract": config.trading.contract,
            "amount": config.trading.amount,
            "cleanup_ref": cleanup_ref,
        },
    )


def _latest_is_stale(bars: list[Bar], config: AppConfig) -> bool:
    if not bars:
        return True
    duration = PERIOD_DURATION[config.strategy.period_type]
    latest = bars[-1].time_ms / 1000
    return time.time() - latest > duration.total_seconds() * 2


async def live_algo(config: AppConfig, mode: str, execute: bool) -> CommandResult:
    if mode == "live-execute":
        assert_live_execution_enabled(execute=execute)
    deadline = asyncio.get_running_loop().time() + config.trading.max_runtime_seconds
    fingerprint = account_fingerprint(config.secrets.api_key)
    manager: ExecutionManager | None = None
    journal: Journal | None = None
    last_bar_time = -1
    owned_side: PositionSide | None = None
    unrelated_position_count = 0
    async with make_client(config, require_chart=True) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        if mode == "live-execute":
            manager = ExecutionManager(client=client, journal_path=_journal_path(config))
            unrelated_position_count = len(await client.get_positions())
            if unrelated_position_count:
                print(
                    f"Warning: account has {unrelated_position_count} unrelated open "
                    "position(s); this run will not manage them.",
                    file=sys.stderr,
                )
        try:
            while asyncio.get_running_loop().time() < deadline:
                bars = await client.get_completed_bars(
                    contract=config.trading.contract,
                    period_type=config.strategy.period_type,
                    count=config.trading.bar_count,
                )
                if _latest_is_stale(bars, config):
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.INCONCLUSIVE,
                        "Completed market data is stale or missing.",
                        {"mode": mode},
                    )
                if bars[-1].time_ms == last_bar_time:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                last_bar_time = bars[-1].time_ms
                event = latest_signal(
                    bars,
                    rsi_period=config.strategy.rsi_period,
                    position_side=owned_side,
                    oversold=config.strategy.oversold,
                    overbought=config.strategy.overbought,
                    exit_level=config.strategy.exit_level,
                )
                if event is None:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                if mode == "live-observe":
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.SUCCESS,
                        "Live RSI signal observed; no order was submitted.",
                        {
                            "mode": mode,
                            "proposed_event": _event_data(AlgoResult(Outcome.SUCCESS, [event], ""))[
                                0
                            ],
                        },
                    )
                if manager is None:
                    raise RuntimeError("Execution manager was not initialized.")
                if event.signal in {Signal.OPEN_BUY, Signal.OPEN_SELL} and owned_side is None:
                    _write_execution_summary(
                        config,
                        fingerprint,
                        "BUY" if event.signal is Signal.OPEN_BUY else "SELL",
                    )
                    journal = await manager.open_position(
                        execute=execute,
                        account_fingerprint=fingerprint,
                        contract=config.trading.contract,
                        amount=config.trading.amount,
                        buy=event.signal is Signal.OPEN_BUY,
                        client_order_id=_client_order_id(),
                    )
                    owned_side = (
                        PositionSide.LONG if event.signal is Signal.OPEN_BUY else PositionSide.SHORT
                    )
                elif event.signal in {Signal.CLOSE_BUY, Signal.CLOSE_SELL} and journal:
                    await manager.cleanup(
                        journal=journal,
                        execute=execute,
                        client_order_id=_client_order_id(),
                    )
                    journal = None
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.SUCCESS,
                        "Live RSI round trip completed and reconciled.",
                        {
                            "mode": mode,
                            "account_fingerprint": fingerprint,
                            "unrelated_position_count": unrelated_position_count,
                        },
                    )
                await asyncio.sleep(config.trading.poll_seconds)
            if journal and manager:
                await manager.cleanup(
                    journal=journal,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
                journal = None
                return CommandResult(
                    "rsi-algo-demo",
                    Outcome.SUCCESS,
                    "Maximum runtime reached; owned position was cleaned up.",
                    {"mode": mode},
                )
        finally:
            journal_path = _journal_path(config)
            pending = Journal.load(journal_path) if journal_path.exists() else None
            if pending and pending.order_ref and manager:
                await manager.cleanup(
                    journal=pending,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
    return CommandResult(
        "rsi-algo-demo",
        Outcome.NO_SIGNAL,
        "No RSI crossing occurred before the runtime limit.",
        {"mode": mode},
    )


async def recover(config: AppConfig, execute: bool) -> CommandResult:
    path = _journal_path(config)
    if not path.exists():
        return CommandResult(
            "recover", Outcome.SUCCESS, "No unresolved execution journal exists.", {}
        )
    journal = Journal.load(path)
    fingerprint = account_fingerprint(config.secrets.api_key)
    if journal.account_fingerprint != fingerprint:
        raise LiveExecutionBlocked("Recovery journal belongs to a different account fingerprint.")
    if not journal.order_ref:
        journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
        return CommandResult(
            "recover",
            Outcome.BLOCKED,
            "Submission may have been accepted, but ownership cannot be confirmed automatically.",
            {
                "state": JournalState.OWNERSHIP_UNCONFIRMED.value,
                "client_order_id": journal.client_order_id,
            },
        )
    async with make_client(config) as client:
        position = await client.get_position_detail(journal.order_ref)
        if position is None:
            journal.clear()
            return CommandResult(
                "recover",
                Outcome.SUCCESS,
                "Tracked position is already closed; journal cleared.",
                {},
            )
        if not execute:
            return CommandResult(
                "recover",
                Outcome.BLOCKED,
                "Tracked position remains open; rerun recovery with both execution gates.",
                {"order_ref": journal.order_ref},
            )
        manager = ExecutionManager(client=client, journal_path=path)
        cleanup_ref = await manager.cleanup(
            journal=journal, execute=True, client_order_id=_client_order_id()
        )
    return CommandResult(
        "recover",
        Outcome.SUCCESS,
        "Tracked position was cleaned up and confirmed closed.",
        {"cleanup_ref": cleanup_ref},
    )
