from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as utc_clock
from importlib.resources import files
from math import isfinite
from pathlib import Path
from typing import Any

from .algo import AlgoResult, Outcome, evaluate_replay, latest_signal
from .api import PERIOD_DURATION, Bar, TraderApiClient
from .config import AppConfig
from .contracts import calculate_amount, find_contract_setting, validate_amount
from .execution import ExecutionManager
from .output import CommandResult
from .price_client import Quote, read_quote
from .safety import (
    Journal,
    JournalState,
    LiveExecutionBlocked,
    LiveRiskState,
    assert_live_execution_enabled,
)
from .sma_algo import (
    SmaBacktestResult,
    SmaSignalEvent,
    SmaTrade,
    evaluate_sma_replay,
    latest_sma_signal,
    synthetic_sma_fixture,
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


def _risk_state_path(config: AppConfig) -> Path:
    name = "".join(
        character.lower() if character.isalnum() else "-" for character in config.trading.contract
    )
    return Path("runtime") / f"risk-{name}.json"


def _utc_date(timestamp: float | None = None) -> str:
    value = timestamp if timestamp is not None else time.time()
    return datetime.fromtimestamp(value, UTC).date().isoformat()


def _account_equity(balance: dict[str, Any]) -> float:
    values = {str(key).lower(): value for key, value in balance.items()}
    for key in ("equity", "balance", "available"):
        raw_value = values.get(key)
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if isfinite(value) and value > 0:
            return value
    raise LiveExecutionBlocked(
        "Live risk controls require a finite positive account equity or balance value."
    )


def _account_level_risk_enabled(config: AppConfig) -> bool:
    return (
        config.trading.max_daily_loss_pct is not None
        or config.trading.max_drawdown_pct is not None
    )


async def _load_live_risk_state(
    config: AppConfig, client: TraderApiClient, fingerprint: str
) -> tuple[LiveRiskState, float]:
    equity = _account_equity(await client.get_account_balance())
    path = _risk_state_path(config)
    today = _utc_date()
    if not path.exists():
        return (
            LiveRiskState.initialize(
                path=path,
                account_fingerprint=fingerprint,
                contract=config.trading.contract,
                utc_date=today,
                equity=equity,
            ),
            equity,
        )

    state = LiveRiskState.load(path)
    if state.account_fingerprint != fingerprint:
        raise LiveExecutionBlocked("Live risk state belongs to a different account fingerprint.")
    if state.contract != config.trading.contract:
        raise LiveExecutionBlocked("Live risk state belongs to a different contract.")
    if state.utc_date != today:
        state = state.with_updates(
            utc_date=today,
            daily_start_equity=equity,
            daily_loss_triggered=False,
            peak_equity=max(state.peak_equity, equity),
        )
    elif equity > state.peak_equity:
        state = state.with_updates(peak_equity=equity)
    return state, equity


def _apply_live_risk_limits(
    state: LiveRiskState,
    *,
    equity: float,
    max_daily_loss_pct: float | None,
    max_drawdown_pct: float | None,
) -> tuple[LiveRiskState, tuple[str, ...]]:
    if not isfinite(equity) or equity <= 0:
        raise LiveExecutionBlocked("Live risk controls received an invalid account equity value.")
    next_state = state
    if equity > next_state.peak_equity:
        next_state = next_state.with_updates(peak_equity=equity)
    triggered: list[str] = []
    if max_daily_loss_pct is not None:
        daily_breach = equity <= next_state.daily_start_equity * (1.0 - max_daily_loss_pct / 100.0)
        if next_state.daily_loss_triggered or daily_breach:
            if not next_state.daily_loss_triggered:
                next_state = next_state.with_updates(daily_loss_triggered=True)
            triggered.append("MAX_DAILY_LOSS")
    if max_drawdown_pct is not None:
        drawdown_breach = equity <= next_state.peak_equity * (1.0 - max_drawdown_pct / 100.0)
        if next_state.drawdown_triggered or drawdown_breach:
            if not next_state.drawdown_triggered:
                next_state = next_state.with_updates(drawdown_triggered=True)
            triggered.append("MAX_DRAWDOWN")
    return next_state, tuple(triggered)


def _live_stop_price(
    *,
    entry_price: float,
    atr: float,
    atr_stop_multiple: float,
    amount: float,
    entry_equity: float | None,
    max_trade_loss_pct: float | None,
) -> float:
    stop_price = entry_price - atr * atr_stop_multiple
    if max_trade_loss_pct is not None:
        if entry_equity is None:
            raise LiveExecutionBlocked(
                "A live per-trade loss limit requires an account equity snapshot."
            )
        risk_budget = entry_equity * max_trade_loss_pct / 100.0
        loss_stop_price = entry_price - risk_budget / amount
        stop_price = max(stop_price, loss_stop_price)
    return stop_price


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


def _sma_event_data(events: list[SmaSignalEvent]) -> list[dict[str, Any]]:
    return [
        {
            "signal_time_ms": event.signal_time_ms,
            "execution_time_ms": event.execution_time_ms,
            "signal": event.signal.value,
            "reason": event.reason,
            "sma_fast": round(event.sma_fast, 8),
            "sma_slow": round(event.sma_slow, 8),
            "atr": round(event.atr, 8),
            "close": event.close,
            "stop_price": (round(event.stop_price, 8) if event.stop_price is not None else None),
        }
        for event in events
    ]


def _sma_trade_data(trades: list[SmaTrade]) -> list[dict[str, Any]]:
    return [
        {
            "entry_time_ms": trade.entry_time_ms,
            "exit_time_ms": trade.exit_time_ms,
            "entry_price": round(trade.entry_price, 8),
            "exit_price": round(trade.exit_price, 8),
            "amount": trade.amount,
            "gross_pnl": round(trade.gross_pnl, 8),
            "commission": round(trade.commission, 8),
            "financing": round(trade.financing, 8),
            "net_pnl": round(trade.net_pnl, 8),
            "exit_reason": trade.exit_reason,
        }
        for trade in trades
    ]


def _sma_result_data(result: SmaBacktestResult) -> dict[str, Any]:
    return {
        "events": _sma_event_data(result.events),
        "trades": _sma_trade_data(result.trades),
        "metrics": {
            "final_equity": round(result.final_equity, 8),
            "buy_and_hold_equity": round(result.buy_and_hold_equity, 8),
            "strategy_return": round(result.strategy_return, 8),
            "buy_and_hold_return": round(result.buy_and_hold_return, 8),
            "strategy_sharpe": (
                round(result.strategy_sharpe, 8) if result.strategy_sharpe is not None else None
            ),
            "buy_and_hold_sharpe": (
                round(result.buy_and_hold_sharpe, 8)
                if result.buy_and_hold_sharpe is not None
                else None
            ),
            "strategy_max_drawdown": round(result.strategy_max_drawdown, 8),
            "buy_and_hold_max_drawdown": round(result.buy_and_hold_max_drawdown, 8),
            "risk_limit_triggers": result.risk_limit_triggers,
            "blocked_entry_count": result.blocked_entry_count,
        },
    }


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


async def export_sma_bars(
    config: AppConfig, output_path: Path, count: int | None = None
) -> CommandResult:
    requested_count = config.trading.bar_count if count is None else count
    if requested_count <= 0:
        raise ValueError("count must be greater than zero.")
    async with make_client(config, require_chart=True) as client:
        bars = await client.get_completed_bars(
            contract=config.trading.contract,
            period_type=config.strategy.sma_period_type,
            count=requested_count,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            [
                {
                    "time": bar.time_ms,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                }
                for bar in bars
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    required_count = max(
        config.strategy.sma_slow_period + 1,
        config.strategy.atr_period + 2,
    )
    outcome = Outcome.SUCCESS if len(bars) >= required_count else Outcome.INCONCLUSIVE
    if len(bars) < required_count:
        message = "Bars exported, but there are not enough completed bars for SMA/ATR."
    elif len(bars) < requested_count:
        message = "Exported all bars returned by the chart service; fewer than requested."
    else:
        message = "Completed SMA bars exported."
    return CommandResult(
        "sma-data-export",
        outcome,
        message,
        {
            "contract": config.trading.contract,
            "period_type": config.strategy.sma_period_type,
            "requested_count": requested_count,
            "completed_bar_count": len(bars),
            "required_bar_count": required_count,
            "returned_less_than_requested": len(bars) < requested_count,
            "output_file": str(output_path),
        },
    )


def _load_bars_fixture(path: Path) -> list[Bar]:
    payload = json.loads(path.read_text(encoding="utf-8"))
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


def load_replay_fixture(path: Path | None) -> list[Bar]:
    fixture_path = path or Path(
        str(files("trader_api_examples") / "fixtures" / "rsi_reversal.json")
    )
    return _load_bars_fixture(fixture_path)


def load_sma_replay_fixture(path: Path | None) -> list[Bar]:
    if path is None:
        return synthetic_sma_fixture()
    if path.suffix.lower() == ".csv":
        return _load_sma_csv_fixture(path)
    return _load_bars_fixture(path)


def _load_sma_csv_fixture(path: Path) -> list[Bar]:
    required_columns = ("<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>")
    bars: list[Bar] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if reader.fieldnames is None or any(
                column not in reader.fieldnames for column in required_columns
            ):
                raise ValueError(
                    "CSV replay fixture must contain <DATE>, <TIME>, <OPEN>, <HIGH>, "
                    "<LOW>, and <CLOSE> columns."
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    timestamp = datetime.strptime(
                        f"{row['<DATE>'].strip()} {row['<TIME>'].strip()}",
                        "%Y.%m.%d %H:%M:%S",
                    ).replace(tzinfo=UTC)
                    bars.append(
                        Bar(
                            time_ms=int(timestamp.timestamp() * 1000),
                            open=float(row["<OPEN>"]),
                            high=float(row["<HIGH>"]),
                            low=float(row["<LOW>"]),
                            close=float(row["<CLOSE>"]),
                        )
                    )
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"Invalid CSV replay row at line {line_number}.") from error
    except FileNotFoundError as error:
        raise ValueError(f"Replay fixture not found: {path}") from error
    if not bars:
        raise ValueError("CSV replay fixture contains no bars.")
    return bars


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


def replay_sma_algo(
    config: AppConfig,
    fixture: Path | None,
    *,
    buffer_override: float | None = None,
    stop_override: float | None = None,
    amount_override: float | None = None,
    annualization_factor_override: float | None = None,
    commission_rate_override: float | None = None,
    slippage_bps_override: float | None = None,
    commission_per_unit_override: float | None = None,
    commission_round_turn_per_lot_override: float | None = None,
    amount_per_lot_override: float | None = None,
    spread_bps_override: float | None = None,
    financing_bps_per_day_override: float | None = None,
    market_impact_bps_override: float | None = None,
    max_holding_hours: float | None = None,
    max_trade_loss_pct: float | None = None,
    max_daily_loss_pct: float | None = None,
    max_drawdown_pct: float | None = None,
    evaluation_start_index: int = 0,
    evaluation_end_index: int | None = None,
) -> CommandResult:
    atr_buffer = config.strategy.sma_exit_buffer_atr if buffer_override is None else buffer_override
    atr_stop_multiple = (
        config.strategy.atr_stop_multiple if stop_override is None else stop_override
    )
    amount = config.trading.amount if amount_override is None else amount_override
    annualization_factor = (
        _sma_annualization_factor(config.strategy.sma_period_type)
        if annualization_factor_override is None
        else annualization_factor_override
    )
    commission_rate = (
        config.strategy.commission_rate
        if commission_rate_override is None
        else commission_rate_override
    )
    slippage_bps = (
        config.strategy.slippage_bps
        if slippage_bps_override is None
        else slippage_bps_override
    )
    commission_per_unit = (
        config.strategy.commission_per_unit
        if commission_per_unit_override is None
        else commission_per_unit_override
    )
    commission_round_turn_per_lot = (
        config.strategy.commission_round_turn_per_lot
        if commission_round_turn_per_lot_override is None
        else commission_round_turn_per_lot_override
    )
    amount_per_lot = (
        config.trading.amount_per_lot
        if amount_per_lot_override is None
        else amount_per_lot_override
    )
    spread_bps = config.strategy.spread_bps if spread_bps_override is None else spread_bps_override
    financing_bps_per_day = (
        config.strategy.financing_bps_per_day
        if financing_bps_per_day_override is None
        else financing_bps_per_day_override
    )
    market_impact_bps = (
        config.strategy.market_impact_bps
        if market_impact_bps_override is None
        else market_impact_bps_override
    )
    bars = load_sma_replay_fixture(fixture)
    resolved_end_index = len(bars) if evaluation_end_index is None else evaluation_end_index
    result = evaluate_sma_replay(
        bars,
        fast_period=config.strategy.sma_fast_period,
        slow_period=config.strategy.sma_slow_period,
        atr_period=config.strategy.atr_period,
        atr_buffer=atr_buffer,
        atr_stop_multiple=atr_stop_multiple,
        amount=amount,
        commission_rate=commission_rate,
        slippage_bps=slippage_bps,
        commission_per_unit=commission_per_unit,
        commission_round_turn_per_lot=commission_round_turn_per_lot,
        amount_per_lot=amount_per_lot,
        spread_bps=spread_bps,
        financing_bps_per_day=financing_bps_per_day,
        market_impact_bps=market_impact_bps,
        max_holding_hours=max_holding_hours,
        max_trade_loss_pct=max_trade_loss_pct,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        annualization_factor=annualization_factor,
        evaluation_start_index=evaluation_start_index,
        evaluation_end_index=resolved_end_index,
    )
    evaluation: dict[str, Any] = {
        "start_index": evaluation_start_index,
        "end_index": resolved_end_index,
        "warmup_bar_count": evaluation_start_index,
    }
    if bars and 0 <= evaluation_start_index < len(bars) and 0 < resolved_end_index <= len(bars):
        evaluation.update(
            {
                "start_time_ms": bars[evaluation_start_index].time_ms,
                "end_time_ms": bars[resolved_end_index - 1].time_ms,
            }
        )
    return CommandResult(
        "sma-algo-demo",
        result.outcome,
        result.message,
        {
            "mode": "replay",
            "contract": config.trading.contract,
            "period_type": config.strategy.sma_period_type,
            "parameters": {
                "fast_period": config.strategy.sma_fast_period,
                "slow_period": config.strategy.sma_slow_period,
                "atr_period": config.strategy.atr_period,
                "atr_buffer": atr_buffer,
                "atr_stop_multiple": atr_stop_multiple,
                "amount": amount,
                "commission_rate": commission_rate,
                "slippage_bps": slippage_bps,
                "commission_per_unit": commission_per_unit,
                "commission_round_turn_per_lot": commission_round_turn_per_lot,
                "amount_per_lot": amount_per_lot,
                "spread_bps": spread_bps,
                "financing_bps_per_day": financing_bps_per_day,
                "market_impact_bps": market_impact_bps,
                "max_holding_hours": max_holding_hours,
                "max_trade_loss_pct": max_trade_loss_pct,
                "max_daily_loss_pct": max_daily_loss_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "annualization_factor": annualization_factor,
                "execution": "next_tradable_bar",
            },
            "evaluation": evaluation,
            **_sma_result_data(result),
        },
    )


async def order_lifecycle(config: AppConfig, execute: bool) -> CommandResult:
    assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    fingerprint = account_fingerprint(config.secrets.api_key)
    journal_path = _journal_path(config)
    journal: Journal | None = None
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        manager = ExecutionManager(
            client=client,
            journal_path=journal_path,
            live_trading_enabled=config.live_trading_enabled,
        )
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


def _audit_quote(quote: Quote, observed_at_ms: int) -> dict[str, Any]:
    midpoint = (quote.bid + quote.ask) / 2.0
    spread_price = quote.ask - quote.bid
    return {
        "observed_at_ms": observed_at_ms,
        "contract": quote.contract,
        "bid": quote.bid,
        "ask": quote.ask,
        "midpoint": midpoint,
        "spread_price": spread_price,
        "spread_bps": (spread_price / midpoint * 10_000.0 if midpoint else None),
    }


def _audit_contract_setting(setting: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "market",
        "nameEng",
        "baseCurr",
        "counterCurr",
        "contractSize",
        "decimalPlace",
        "minTradeLot",
        "minLotIncrementUnit",
        "askInterest",
        "bidInterest",
        "interestMethod",
        "storage",
        "orderPips",
        "enabled",
    )
    return {field: setting[field] for field in fields if field in setting}


def _audit_order_state(orders: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "working_order_count": len(orders["workingOrders"]),
        "executed_order_count": len(orders["executedOrders"]),
        "cancelled_order_count": len(orders["cancelledOrders"]),
        "working_orders": orders["workingOrders"],
        "executed_orders": orders["executedOrders"],
        "cancelled_orders": orders["cancelledOrders"],
    }


def _audit_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "ref",
        "orderRef",
        "contract",
        "contractCode",
        "market",
        "amount",
        "side",
        "buyOrSell",
        "openPrice",
        "price",
        "entryPrice",
        "time",
    )
    return [
        {field: position[field] for field in fields if field in position} for position in positions
    ]


def _validate_audit_inputs(
    *,
    config: AppConfig,
    mode: str,
    execute: bool,
    amount: float | None,
    hold_seconds: float,
    samples: int,
    interval_seconds: float,
) -> float:
    if mode not in {"snapshot", "close-existing", "round-trip"}:
        raise ValueError("mode must be 'snapshot', 'close-existing', or 'round-trip'.")
    if samples < 1 or samples > 3600:
        raise ValueError("samples must be between 1 and 3600.")
    if interval_seconds < 0 or interval_seconds > 3600:
        raise ValueError("interval_seconds must be between zero and 3600.")
    if hold_seconds < 0 or hold_seconds > 600:
        raise ValueError("hold_seconds must be between zero and 600.")
    if mode == "snapshot":
        if execute:
            raise LiveExecutionBlocked("Snapshot mode never submits orders; omit --execute.")
    else:
        if config.environment != "demo":
            raise LiveExecutionBlocked("Round-trip audit is restricted to environment: demo.")
        assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    resolved_amount = config.trading.amount if amount is None else amount
    if resolved_amount <= 0:
        raise ValueError("amount must be greater than zero.")
    return resolved_amount


async def _collect_audit_quotes(
    config: AppConfig, *, samples: int, interval_seconds: float
) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    for index in range(samples):
        quote = await read_quote(config)
        quotes.append(_audit_quote(quote, int(time.time() * 1000)))
        if index + 1 < samples and interval_seconds:
            await asyncio.sleep(interval_seconds)
    return quotes


async def _close_existing_demo_positions(
    client: TraderApiClient,
    *,
    config: AppConfig,
    positions: list[dict[str, Any]],
    balance_before: dict[str, Any],
    orders_before: dict[str, list[dict[str, Any]]],
) -> CommandResult:
    targets: list[tuple[str, float]] = []
    for position in positions:
        contract = str(
            position.get("contract") or position.get("contractCode") or position.get("market") or ""
        ).upper()
        if contract != config.trading.contract.upper():
            raise LiveExecutionBlocked(
                "close-existing will not touch positions outside configured contract "
                f"{config.trading.contract}: found {contract or 'unknown'}."
            )
        reference = position.get("ref") or position.get("orderRef")
        raw_amount = position.get("amount")
        if reference is None or raw_amount is None:
            raise RuntimeError("An existing position did not include a reference and amount.")
        try:
            position_amount = float(raw_amount)
        except (TypeError, ValueError) as error:
            raise RuntimeError("An existing position amount was invalid.") from error
        if position_amount <= 0:
            raise RuntimeError("An existing position amount must be greater than zero.")
        targets.append((str(reference), position_amount))

    liquidation_refs: list[dict[str, Any]] = []
    for reference, position_amount in targets:
        liquidation_ref = await client.liquidate_market_deal(
            order_ref=reference,
            amount=position_amount,
            client_order_id=_client_order_id(),
        )
        liquidation_refs.append(
            {
                "position_ref": reference,
                "amount": position_amount,
                "liquidation_ref": liquidation_ref,
            }
        )

    remaining: list[dict[str, Any]] = []
    target_refs = {reference for reference, _ in targets}
    for attempt in range(10):
        remaining = await client.get_positions()
        if not any(
            str(position.get("ref") or position.get("orderRef")) in target_refs
            for position in remaining
        ):
            break
        if attempt < 9:
            await asyncio.sleep(1.0)
    else:
        raise RuntimeError("Liquidation was submitted but the target positions remain open.")

    return CommandResult(
        "execution-cost-audit",
        Outcome.SUCCESS,
        "Existing demo LLG positions were liquidated and verified flat.",
        {
            "mode": "close-existing",
            "submitted_order": True,
            "environment": config.environment,
            "contract": config.trading.contract,
            "positions_before": _audit_positions(positions),
            "open_position_count_before": len(positions),
            "balance_before": balance_before,
            "order_state_before": _audit_order_state(orders_before),
            "liquidations": liquidation_refs,
            "positions_after": _audit_positions(remaining),
            "open_position_count_after": len(remaining),
            "balance_after": await client.get_account_balance(),
            "order_state_after": _audit_order_state(await client.get_orders()),
        },
    )


def _quote_summary(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    spreads = [float(quote["spread_bps"]) for quote in quotes if quote["spread_bps"] is not None]
    prices = [float(quote["spread_price"]) for quote in quotes]
    if not spreads:
        return {"sample_count": len(quotes), "spread_bps": None, "spread_price": None}
    spreads.sort()
    prices.sort()
    middle = len(spreads) // 2
    median_spread = (
        spreads[middle] if len(spreads) % 2 else (spreads[middle - 1] + spreads[middle]) / 2.0
    )
    middle_price = len(prices) // 2
    median_price = (
        prices[middle_price]
        if len(prices) % 2
        else (prices[middle_price - 1] + prices[middle_price]) / 2.0
    )
    return {
        "sample_count": len(quotes),
        "minimum_spread_bps": min(spreads),
        "median_spread_bps": median_spread,
        "maximum_spread_bps": max(spreads),
        "minimum_spread_price": min(prices),
        "median_spread_price": median_price,
        "maximum_spread_price": max(prices),
    }


async def execution_cost_audit(
    config: AppConfig,
    *,
    mode: str,
    execute: bool,
    amount: float | None,
    hold_seconds: float,
    samples: int,
    interval_seconds: float,
    order_ref: str | None,
) -> CommandResult:
    resolved_amount = _validate_audit_inputs(
        config=config,
        mode=mode,
        execute=execute,
        amount=amount,
        hold_seconds=hold_seconds,
        samples=samples,
        interval_seconds=interval_seconds,
    )
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=resolved_amount, contract_setting=setting)
        balance_before = await client.get_account_balance()
        positions_before = await client.get_positions()
        orders_before = await client.get_orders()
        if mode == "close-existing":
            return await _close_existing_demo_positions(
                client,
                config=config,
                positions=positions_before,
                balance_before=balance_before,
                orders_before=orders_before,
            )
        quotes = await _collect_audit_quotes(
            config, samples=samples, interval_seconds=interval_seconds
        )
        data: dict[str, Any] = {
            "mode": mode,
            "submitted_order": False,
            "environment": config.environment,
            "contract": config.trading.contract,
            "amount": resolved_amount,
            "contract_setting": _audit_contract_setting(setting),
            "balance_before": balance_before,
            "open_position_count_before": len(positions_before),
            "positions_before": _audit_positions(positions_before),
            "order_state_before": _audit_order_state(orders_before),
            "quotes": quotes,
            "quote_summary": _quote_summary(quotes),
            "unmodeled_costs": [
                "financing/swap units",
                "market impact",
            ],
        }
        if order_ref:
            data["requested_position_detail"] = await client.get_position_detail(order_ref)
        if mode == "snapshot":
            return CommandResult(
                "execution-cost-audit",
                Outcome.SUCCESS,
                "Read-only execution-cost audit snapshot collected; no order was submitted.",
                data,
            )
        if positions_before:
            raise LiveExecutionBlocked(
                "Round-trip audit requires no existing open positions; "
                f"found {len(positions_before)}. No order was submitted."
            )

        fingerprint = account_fingerprint(config.secrets.api_key)
        journal_path = _journal_path(config)
        manager = ExecutionManager(
            client=client,
            journal_path=journal_path,
            live_trading_enabled=config.live_trading_enabled,
        )
        journal: Journal | None = None
        cleanup_ref: str | None = None
        try:
            entry_quote = await read_quote(config)
            journal = await manager.open_position(
                execute=execute,
                account_fingerprint=fingerprint,
                contract=config.trading.contract,
                amount=resolved_amount,
                buy=True,
                client_order_id=_client_order_id(),
            )
            data["submitted_order"] = True
            data["entry_order_ref"] = journal.order_ref
            data["entry_reference_quote"] = _audit_quote(entry_quote, int(time.time() * 1000))
            data["position_detail_after_entry"] = (
                await client.get_position_detail(journal.order_ref) if journal.order_ref else None
            )
            if hold_seconds:
                await asyncio.sleep(hold_seconds)
            exit_quote = await read_quote(config)
            cleanup_ref = await manager.cleanup(
                journal=journal,
                execute=execute,
                client_order_id=_client_order_id(),
            )
            journal = None
            data["exit_reference_quote"] = _audit_quote(exit_quote, int(time.time() * 1000))
            data["cleanup_ref"] = cleanup_ref
            data["liquidation_response"] = client.last_liquidation_payload
            data["balance_after"] = await client.get_account_balance()
            data["order_state_after"] = _audit_order_state(await client.get_orders())
            data["entry_position_detail_after_cleanup"] = await client.get_position_detail(
                data["entry_order_ref"]
            )
        finally:
            pending = Journal.load(journal_path) if journal_path.exists() else None
            if pending and pending.order_ref:
                await manager.cleanup(
                    journal=pending,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
        return CommandResult(
            "execution-cost-audit",
            Outcome.SUCCESS,
            "Demo execution-cost round trip completed and reconciled.",
            data,
        )


@dataclass(frozen=True)
class _MarketDataHealth:
    latest_bar_age_seconds: float | None
    stale_latest_bar: bool
    unexpected_internal_gap: bool
    gap_start_time_ms: int | None = None
    gap_end_time_ms: int | None = None
    missing_bar_count: int | None = None

    @property
    def unusable(self) -> bool:
        return self.stale_latest_bar or self.unexpected_internal_gap

    @property
    def issue(self) -> str | None:
        if self.stale_latest_bar:
            return "STALE_OR_MISSING_LATEST_BAR"
        if self.unexpected_internal_gap:
            return "UNEXPECTED_INTERNAL_GAP"
        return None


def _clock_minutes(value: str | None) -> int | None:
    if value is None:
        return None
    parsed = utc_clock.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def _is_closed_bar_start(
    time_ms: int,
    *,
    period_type: int,
    daily_break_start_utc: str | None,
    daily_break_end_utc: str | None,
    closed_dates_utc: tuple[str, ...],
) -> bool:
    timestamp = datetime.fromtimestamp(time_ms / 1000, tz=UTC)
    if timestamp.date().isoformat() in closed_dates_utc or timestamp.weekday() >= 5:
        return True
    if period_type not in {1, 2}:
        return False
    start = _clock_minutes(daily_break_start_utc)
    end = _clock_minutes(daily_break_end_utc)
    if start is None or end is None or start == end:
        return False
    current = timestamp.hour * 60 + timestamp.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _is_expected_closed_now(
    current_time: float,
    period_type: int,
    *,
    daily_break_start_utc: str | None,
    daily_break_end_utc: str | None,
    closed_dates_utc: tuple[str, ...],
) -> bool:
    current_ms = int(current_time * 1000)
    if _is_closed_bar_start(
        current_ms,
        period_type=period_type,
        daily_break_start_utc=daily_break_start_utc,
        daily_break_end_utc=daily_break_end_utc,
        closed_dates_utc=closed_dates_utc,
    ):
        return True
    # Chart timestamps identify bar starts. During the first interval after a
    # session reopens, the new bar is still forming and is not yet available.
    if period_type in {1, 2}:
        duration_ms = int(PERIOD_DURATION[period_type].total_seconds() * 1000)
        return _is_closed_bar_start(
            current_ms - duration_ms,
            period_type=period_type,
            daily_break_start_utc=daily_break_start_utc,
            daily_break_end_utc=daily_break_end_utc,
            closed_dates_utc=closed_dates_utc,
        )
    return False


def _find_unexpected_internal_gap(
    bars: list[Bar],
    period_type: int,
    *,
    daily_break_start_utc: str | None,
    daily_break_end_utc: str | None,
    closed_dates_utc: tuple[str, ...],
) -> tuple[int, int, int] | None:
    duration_ms = int(PERIOD_DURATION[period_type].total_seconds() * 1000)
    for previous, current in zip(bars[:-1], bars[1:], strict=True):
        delta_ms = current.time_ms - previous.time_ms
        if delta_ms <= duration_ms:
            continue
        if delta_ms % duration_ms:
            return previous.time_ms, current.time_ms, 1
        missing_count = delta_ms // duration_ms - 1
        # A very long gap is not worth expanding into millions of timestamps. It is
        # already a data-quality failure, even if the market calendar is incomplete.
        if missing_count > 10_000:
            return previous.time_ms, current.time_ms, missing_count
        all_closed = all(
            _is_closed_bar_start(
                previous.time_ms + offset * duration_ms,
                period_type=period_type,
                daily_break_start_utc=daily_break_start_utc,
                daily_break_end_utc=daily_break_end_utc,
                closed_dates_utc=closed_dates_utc,
            )
            for offset in range(1, missing_count + 1)
        )
        if not all_closed:
            return previous.time_ms, current.time_ms, missing_count
    return None


def _market_data_health(
    bars: list[Bar],
    period_type: int,
    *,
    now: float | None = None,
    daily_break_start_utc: str | None = "23:00",
    daily_break_end_utc: str | None = "01:00",
    closed_dates_utc: tuple[str, ...] = (),
) -> _MarketDataHealth:
    current_time = time.time() if now is None else now
    if not bars:
        return _MarketDataHealth(
            latest_bar_age_seconds=None,
            stale_latest_bar=True,
            unexpected_internal_gap=False,
        )
    duration_seconds = PERIOD_DURATION[period_type].total_seconds()
    latest_age_seconds = max(0.0, current_time - bars[-1].time_ms / 1000)
    stale_latest_bar = (
        latest_age_seconds > duration_seconds * 2
        and not _is_expected_closed_now(
            current_time,
            period_type,
            daily_break_start_utc=daily_break_start_utc,
            daily_break_end_utc=daily_break_end_utc,
            closed_dates_utc=closed_dates_utc,
        )
    )
    gap = _find_unexpected_internal_gap(
        bars,
        period_type,
        daily_break_start_utc=daily_break_start_utc,
        daily_break_end_utc=daily_break_end_utc,
        closed_dates_utc=closed_dates_utc,
    )
    return _MarketDataHealth(
        latest_bar_age_seconds=latest_age_seconds,
        stale_latest_bar=stale_latest_bar,
        unexpected_internal_gap=gap is not None,
        gap_start_time_ms=None if gap is None else gap[0],
        gap_end_time_ms=None if gap is None else gap[1],
        missing_bar_count=None if gap is None else gap[2],
    )


def _latest_is_stale(
    bars: list[Bar],
    period_type: int,
    *,
    now: float | None = None,
    daily_break_start_utc: str | None = "23:00",
    daily_break_end_utc: str | None = "01:00",
    closed_dates_utc: tuple[str, ...] = (),
) -> bool:
    return _market_data_health(
        bars,
        period_type,
        now=now,
        daily_break_start_utc=daily_break_start_utc,
        daily_break_end_utc=daily_break_end_utc,
        closed_dates_utc=closed_dates_utc,
    ).unusable


def _period_label(period_type: int) -> str:
    return {1: "one-minute", 2: "hourly", 3: "daily"}.get(period_type, f"period type {period_type}")


def _sma_annualization_factor(period_type: int) -> float:
    bars_per_day = {1: 24.0 * 60.0, 2: 24.0, 3: 1.0}[period_type]
    return 252.0 * bars_per_day


def _market_data_details(
    bars: list[Bar],
    period_type: int,
    *,
    health: _MarketDataHealth | None = None,
    daily_break_start_utc: str | None = "23:00",
    daily_break_end_utc: str | None = "01:00",
    closed_dates_utc: tuple[str, ...] = (),
) -> dict[str, Any]:
    health = health or _market_data_health(
        bars,
        period_type,
        daily_break_start_utc=daily_break_start_utc,
        daily_break_end_utc=daily_break_end_utc,
        closed_dates_utc=closed_dates_utc,
    )
    latest_time_ms = bars[-1].time_ms if bars else None
    return {
        "period_type": period_type,
        "period": _period_label(period_type),
        "completed_bar_count": len(bars),
        "latest_bar_time_ms": latest_time_ms,
        "latest_bar_age_seconds": (
            None
            if health.latest_bar_age_seconds is None
            else round(health.latest_bar_age_seconds, 3)
        ),
        "market_data_issue": health.issue,
        "gap_start_time_ms": health.gap_start_time_ms,
        "gap_end_time_ms": health.gap_end_time_ms,
        "missing_bar_count": health.missing_bar_count,
    }


def _market_data_issue_message(period_type: int, health: _MarketDataHealth) -> str:
    period = _period_label(period_type)
    if health.unexpected_internal_gap:
        count = (
            f" of {health.missing_bar_count} bar(s)"
            if health.missing_bar_count is not None
            else ""
        )
        return f"Completed {period} market data contains an unexpected internal gap{count}."
    return f"Completed {period} market data is stale or missing."


async def live_algo(config: AppConfig, mode: str, execute: bool) -> CommandResult:
    if mode == "live-execute":
        assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
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
            manager = ExecutionManager(
                client=client,
                journal_path=_journal_path(config),
                live_trading_enabled=config.live_trading_enabled,
            )
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
                health = _market_data_health(
                    bars,
                    config.strategy.period_type,
                    daily_break_start_utc=config.trading.market_data_daily_break_start_utc,
                    daily_break_end_utc=config.trading.market_data_daily_break_end_utc,
                    closed_dates_utc=config.trading.market_data_closed_dates_utc,
                )
                if health.unusable:
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.INCONCLUSIVE,
                        _market_data_issue_message(config.strategy.period_type, health),
                        {
                            "mode": mode,
                            **_market_data_details(
                                bars,
                                config.strategy.period_type,
                                health=health,
                                daily_break_start_utc=(
                                    config.trading.market_data_daily_break_start_utc
                                ),
                                daily_break_end_utc=config.trading.market_data_daily_break_end_utc,
                                closed_dates_utc=config.trading.market_data_closed_dates_utc,
                            ),
                        },
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


async def live_sma_algo(config: AppConfig, mode: str, execute: bool) -> CommandResult:
    if mode == "live-execute":
        assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    runtime_deadline = asyncio.get_running_loop().time() + config.trading.max_runtime_seconds
    fingerprint = account_fingerprint(config.secrets.api_key)
    manager: ExecutionManager | None = None
    journal: Journal | None = None
    last_bar_time = -1
    owned_side: PositionSide | None = None
    stop_price: float | None = None
    entry_time_ms: int | None = None
    unrelated_position_count = 0
    risk_state: LiveRiskState | None = None
    async with make_client(config, require_chart=True) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        if mode == "live-execute":
            manager = ExecutionManager(
                client=client,
                journal_path=_journal_path(config),
                live_trading_enabled=config.live_trading_enabled,
            )
            journal_path = _journal_path(config)
            if journal_path.exists():
                pending = Journal.load(journal_path)
                if pending.account_fingerprint != fingerprint:
                    raise LiveExecutionBlocked(
                        "Execution journal belongs to a different account fingerprint."
                    )
                if pending.contract != config.trading.contract:
                    raise LiveExecutionBlocked(
                        "Execution journal belongs to a different contract."
                    )
                if pending.state is not JournalState.OPEN or not pending.order_ref:
                    raise LiveExecutionBlocked(
                        "An unresolved execution journal requires recovery before "
                        "live SMA management."
                    )
                if (
                    pending.side != "BUY"
                    or pending.entry_time_ms is None
                    or pending.stop_price is None
                ):
                    raise LiveExecutionBlocked(
                        "Tracked SMA position lacks persistent entry or stop state; run recovery."
                    )
                if await client.get_position_detail(pending.order_ref) is None:
                    pending.clear()
                else:
                    journal = pending
                    owned_side = PositionSide.LONG
                    entry_time_ms = pending.entry_time_ms
                    stop_price = pending.stop_price

            positions = await client.get_positions()
            tracked_ref = journal.order_ref if journal is not None else None
            unrelated_position_count = sum(
                1
                for position in positions
                if str(position.get("ref") or position.get("orderRef")) != tracked_ref
            )
            if unrelated_position_count:
                return CommandResult(
                    "sma-algo-demo",
                    Outcome.BLOCKED,
                    "Live SMA execution is blocked by unrelated open position(s).",
                    {
                        "mode": mode,
                        "account_fingerprint": fingerprint,
                        "unrelated_position_count": unrelated_position_count,
                        "max_open_positions": 1,
                    },
                )
            if _account_level_risk_enabled(config):
                risk_state, _ = await _load_live_risk_state(config, client, fingerprint)
        try:
            while True:
                if (
                    owned_side is None
                    and asyncio.get_running_loop().time() >= runtime_deadline
                ):
                    return CommandResult(
                        "sma-algo-demo",
                        Outcome.NO_SIGNAL,
                        "No SMA crossover occurred before the flat runtime limit.",
                        {
                            "mode": mode,
                            "max_runtime_seconds": config.trading.max_runtime_seconds,
                        },
                    )
                if risk_state is not None:
                    risk_state, equity = await _load_live_risk_state(
                        config, client, fingerprint
                    )
                    risk_state, risk_reasons = _apply_live_risk_limits(
                        risk_state,
                        equity=equity,
                        max_daily_loss_pct=config.trading.max_daily_loss_pct,
                        max_drawdown_pct=config.trading.max_drawdown_pct,
                    )
                    if risk_reasons:
                        if owned_side is PositionSide.LONG:
                            if manager is None or journal is None:
                                raise RuntimeError(
                                    "A triggered live risk limit has no owned-position journal."
                                )
                            await manager.cleanup(
                                journal=journal,
                                execute=execute,
                                client_order_id=_client_order_id(),
                            )
                            journal = None
                            owned_side = None
                            stop_price = None
                            entry_time_ms = None
                        return CommandResult(
                            "sma-algo-demo",
                            Outcome.BLOCKED,
                            "Live SMA risk limit triggered; trading is blocked until "
                            "the limit resets.",
                            {
                                "mode": mode,
                                "account_fingerprint": fingerprint,
                                "risk_limits": list(risk_reasons),
                                "equity": equity,
                                "daily_start_equity": risk_state.daily_start_equity,
                                "peak_equity": risk_state.peak_equity,
                                "max_daily_loss_pct": config.trading.max_daily_loss_pct,
                                "max_drawdown_pct": config.trading.max_drawdown_pct,
                                "unrelated_position_count": unrelated_position_count,
                            },
                        )
                bars = await client.get_completed_bars(
                    contract=config.trading.contract,
                    period_type=config.strategy.sma_period_type,
                    count=config.trading.bar_count,
                )
                health = _market_data_health(
                    bars,
                    config.strategy.sma_period_type,
                    daily_break_start_utc=config.trading.market_data_daily_break_start_utc,
                    daily_break_end_utc=config.trading.market_data_daily_break_end_utc,
                    closed_dates_utc=config.trading.market_data_closed_dates_utc,
                )
                if health.unusable:
                    return CommandResult(
                        "sma-algo-demo",
                        Outcome.INCONCLUSIVE,
                        _market_data_issue_message(config.strategy.sma_period_type, health),
                        {
                            "mode": mode,
                            "contract": config.trading.contract,
                            **_market_data_details(
                                bars,
                                config.strategy.sma_period_type,
                                health=health,
                                daily_break_start_utc=(
                                    config.trading.market_data_daily_break_start_utc
                                ),
                                daily_break_end_utc=config.trading.market_data_daily_break_end_utc,
                                closed_dates_utc=config.trading.market_data_closed_dates_utc,
                            ),
                        },
                    )

                if owned_side is PositionSide.LONG and stop_price is not None:
                    quote = await read_quote(config)
                    if quote.bid <= stop_price:
                        if manager is None or journal is None:
                            raise RuntimeError("Execution manager was not initialized.")
                        await manager.cleanup(
                            journal=journal,
                            execute=execute,
                            client_order_id=_client_order_id(),
                        )
                        journal = None
                        owned_side = None
                        stop_price = None
                        return CommandResult(
                            "sma-algo-demo",
                            Outcome.SUCCESS,
                            "Live SMA protective stop was triggered and reconciled.",
                            {
                                "mode": mode,
                                "account_fingerprint": fingerprint,
                                "unrelated_position_count": unrelated_position_count,
                            },
                        )

                if (
                    owned_side is PositionSide.LONG
                    and journal is not None
                    and entry_time_ms is not None
                    and config.trading.max_holding_hours is not None
                    and time.time() * 1000 - entry_time_ms
                    >= config.trading.max_holding_hours * 60 * 60 * 1000
                ):
                    if manager is None:
                        raise RuntimeError("Execution manager was not initialized.")
                    await manager.cleanup(
                        journal=journal,
                        execute=execute,
                        client_order_id=_client_order_id(),
                    )
                    journal = None
                    owned_side = None
                    stop_price = None
                    entry_time_ms = None
                    return CommandResult(
                        "sma-algo-demo",
                        Outcome.SUCCESS,
                        "Live SMA maximum holding time reached and reconciled.",
                        {
                            "mode": mode,
                            "account_fingerprint": fingerprint,
                            "max_holding_hours": config.trading.max_holding_hours,
                            "unrelated_position_count": unrelated_position_count,
                        },
                    )

                if bars[-1].time_ms == last_bar_time:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                last_bar_time = bars[-1].time_ms
                event = latest_sma_signal(
                    bars,
                    fast_period=config.strategy.sma_fast_period,
                    slow_period=config.strategy.sma_slow_period,
                    atr_period=config.strategy.atr_period,
                    atr_buffer=config.strategy.sma_exit_buffer_atr,
                    position_side=owned_side,
                )
                if event is None:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                if mode == "live-observe":
                    return CommandResult(
                        "sma-algo-demo",
                        Outcome.SUCCESS,
                        "Live SMA signal observed; no order was submitted.",
                        {
                            "mode": mode,
                            "proposed_event": _sma_event_data([event])[0],
                        },
                    )
                if manager is None:
                    raise RuntimeError("Execution manager was not initialized.")
                if event.signal is Signal.OPEN_BUY and owned_side is None:
                    quote = await read_quote(config)
                    entry_equity: float | None = None
                    if config.trading.max_trade_loss_pct is not None:
                        entry_equity = _account_equity(await client.get_account_balance())
                    stop_price = _live_stop_price(
                        entry_price=quote.ask,
                        atr=event.atr,
                        atr_stop_multiple=config.strategy.atr_stop_multiple,
                        amount=config.trading.amount,
                        entry_equity=entry_equity,
                        max_trade_loss_pct=config.trading.max_trade_loss_pct,
                    )
                    entry_time_ms = int(time.time() * 1000)
                    _write_execution_summary(config, fingerprint, "BUY")
                    journal = await manager.open_position(
                        execute=execute,
                        account_fingerprint=fingerprint,
                        contract=config.trading.contract,
                        amount=config.trading.amount,
                        buy=True,
                        client_order_id=_client_order_id(),
                        entry_time_ms=entry_time_ms,
                        stop_price=stop_price,
                        entry_price=quote.ask,
                        entry_equity=entry_equity,
                    )
                    owned_side = PositionSide.LONG
                elif event.signal is Signal.CLOSE_BUY and journal is not None:
                    await manager.cleanup(
                        journal=journal,
                        execute=execute,
                        client_order_id=_client_order_id(),
                    )
                    journal = None
                    owned_side = None
                    stop_price = None
                    entry_time_ms = None
                    return CommandResult(
                        "sma-algo-demo",
                        Outcome.SUCCESS,
                        "Live SMA round trip completed and reconciled.",
                        {
                            "mode": mode,
                            "account_fingerprint": fingerprint,
                            "unrelated_position_count": unrelated_position_count,
                        },
                    )
                await asyncio.sleep(config.trading.poll_seconds)
        finally:
            journal_path = _journal_path(config)
            pending_cleanup = Journal.load(journal_path) if journal_path.exists() else None
            if pending_cleanup and pending_cleanup.order_ref and manager:
                await manager.cleanup(
                    journal=pending_cleanup,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
    return CommandResult(
        "sma-algo-demo",
        Outcome.NO_SIGNAL,
        "No SMA crossover occurred before the flat runtime limit.",
        {"mode": mode, "max_runtime_seconds": config.trading.max_runtime_seconds},
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
        manager = ExecutionManager(
            client=client,
            journal_path=path,
            live_trading_enabled=config.live_trading_enabled,
        )
        cleanup_ref = await manager.cleanup(
            journal=journal, execute=True, client_order_id=_client_order_id()
        )
    return CommandResult(
        "recover",
        Outcome.SUCCESS,
        "Tracked position was cleaned up and confirmed closed.",
        {"cleanup_ref": cleanup_ref},
    )
