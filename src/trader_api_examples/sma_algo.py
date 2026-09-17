from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from math import isfinite, sqrt

import numpy as np
import talib

from .algo import Outcome
from .api import Bar
from .strategy import PositionSide, Signal


## These classes are used to store information. Names are mostly self-explanatory.
## Stores SMA20 and SMA50 and ATR14 arrays
@dataclass(frozen=True)
class SmaIndicators:
    fast: np.ndarray
    slow: np.ndarray
    atr: np.ndarray


@dataclass(frozen=True)
class SmaSignalEvent:
    signal_time_ms: int
    execution_time_ms: int | None
    signal: Signal
    reason: str
    sma_fast: float
    sma_slow: float
    atr: float
    close: float
    stop_price: float | None = None


@dataclass(frozen=True)
class SmaTrade:
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    amount: float
    gross_pnl: float
    commission: float
    financing: float
    net_pnl: float
    exit_reason: str


## REplay results.
@dataclass(frozen=True)
class SmaBacktestResult:
    outcome: Outcome
    events: list[SmaSignalEvent]
    trades: list[SmaTrade]
    final_equity: float
    buy_and_hold_equity: float
    strategy_return: float
    buy_and_hold_return: float
    strategy_sharpe: float | None
    buy_and_hold_sharpe: float | None
    strategy_max_drawdown: float
    buy_and_hold_max_drawdown: float
    risk_limit_triggers: dict[str, int]
    blocked_entry_count: int
    message: str


## Internal state for the currently open simulated trade.
@dataclass
class _OpenPosition:
    entry_time_ms: int
    entry_price: float
    amount: float
    stop_price: float
    stop_reason: str
    entry_event: SmaSignalEvent


## how many candles are needed before the strategy can work.
def required_bar_count(*, slow_period: int, atr_period: int) -> int:
    _validate_parameters(
        fast_period=1,
        slow_period=slow_period,
        atr_period=atr_period,
        atr_buffer=0.0,
        atr_stop_multiple=1.0,
        amount=1.0,
        commission_rate=0.0,
        slippage_bps=0.0,
    )
    return max(slow_period + 1, atr_period + 2)


## Extracts candles to calculate SMA & ATR values.
def calculate_sma_indicators(
    bars: list[Bar], *, fast_period: int, slow_period: int, atr_period: int
) -> SmaIndicators:
    _validate_parameters(
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
        atr_buffer=0.0,
        atr_stop_multiple=1.0,
        amount=1.0,
        commission_rate=0.0,
        slippage_bps=0.0,
    )
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    highs = np.asarray([bar.high for bar in bars], dtype=np.float64)
    lows = np.asarray([bar.low for bar in bars], dtype=np.float64)
    return SmaIndicators(
        fast=talib.SMA(closes, timeperiod=fast_period),
        slow=talib.SMA(closes, timeperiod=slow_period),
        atr=talib.ATR(highs, lows, closes, timeperiod=atr_period),
    )


## The crossover entry & exit rules. ie SMA50 - (ATR buffer x ATR14)
def latest_sma_signal(
    bars: list[Bar],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    atr_period: int = 14,
    atr_buffer: float = 0.0,
    position_side: PositionSide | None,
) -> SmaSignalEvent | None:
    _validate_parameters(
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
        atr_buffer=atr_buffer,
        atr_stop_multiple=1.0,
        amount=1.0,
        commission_rate=0.0,
        slippage_bps=0.0,
    )
    if len(bars) < required_bar_count(slow_period=slow_period, atr_period=atr_period):
        return None
    _validate_bars(bars)
    indicators = calculate_sma_indicators(
        bars, fast_period=fast_period, slow_period=slow_period, atr_period=atr_period
    )
    return _signal_at(
        bars,
        indicators,
        index=len(bars) - 1,
        position_side=position_side,
        atr_buffer=atr_buffer,
    )


## code for backtesting, takes all the historical data, sma periods etc.
## Read completed candle
## Check whether an earlier exit is pending
## Check whether an earlier entry is pending
## Apply protective stop
## Detect a new crossover at the candle close
## Schedule action for the next candle
## Update equity
## Apply daily-loss and drawdown limits
def evaluate_sma_replay(
    bars: list[Bar],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    atr_period: int = 14,
    atr_buffer: float = 0.0,
    atr_stop_multiple: float = 2.0,
    amount: float = 1.0,
    commission_rate: float = 0.0,
    slippage_bps: float = 0.0,
    commission_per_unit: float = 0.0,
    commission_round_turn_per_lot: float = 0.0,
    amount_per_lot: float = 1.0,
    spread_bps: float = 0.0,
    financing_bps_per_day: float = 0.0,
    market_impact_bps: float = 0.0,
    max_holding_hours: float | None = None,
    max_trade_loss_pct: float | None = None,
    max_daily_loss_pct: float | None = None,
    max_drawdown_pct: float | None = None,
    annualization_factor: float = 252.0,
    evaluation_start_index: int = 0,
    evaluation_end_index: int | None = None,
) -> SmaBacktestResult:
    _validate_parameters(
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
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
    )
    if not isfinite(annualization_factor) or annualization_factor <= 0:
        raise ValueError("annualization_factor must be finite and greater than zero.")
    if len(bars) < required_bar_count(slow_period=slow_period, atr_period=atr_period):
        return _empty_result(Outcome.INCONCLUSIVE, "Not enough completed bars for SMA/ATR.")
    _validate_bars(bars)
    evaluation_end = len(bars) if evaluation_end_index is None else evaluation_end_index
    _validate_evaluation_bounds(
        bar_count=len(bars),
        evaluation_start_index=evaluation_start_index,
        evaluation_end_index=evaluation_end,
        required_count=required_bar_count(slow_period=slow_period, atr_period=atr_period),
    )

    indicators = calculate_sma_indicators(
        bars, fast_period=fast_period, slow_period=slow_period, atr_period=atr_period
    )
    initial_equity = amount * bars[evaluation_start_index].open
    events: list[SmaSignalEvent] = []
    trades: list[SmaTrade] = []
    equity_curve = [initial_equity]
    realized_pnl = 0.0
    position: _OpenPosition | None = None
    pending_entry: tuple[int, SmaSignalEvent] | None = None
    pending_exit: str | None = None
    current_day: date | None = None
    daily_start_equity = initial_equity
    daily_loss_triggered = False
    drawdown_triggered = False
    peak_equity = initial_equity
    risk_limit_triggers = {
        "MAX_TRADE_LOSS": 0,
        "MAX_DAILY_LOSS": 0,
        "MAX_DRAWDOWN": 0,
    }
    blocked_entry_count = 0

    for index in range(evaluation_start_index, evaluation_end):
        bar = bars[index]
        bar_day = datetime.fromtimestamp(bar.time_ms / 1000.0, UTC).date()
        if current_day != bar_day:
            current_day = bar_day
            daily_start_equity = _mark_equity(
                initial_equity,
                realized_pnl,
                position,
                _sell_fill(bar.open, spread_bps=spread_bps),
            )
            daily_loss_triggered = False

        if pending_exit is not None and position is not None:
            trade = _close_position(
                position,
                exit_time_ms=bar.time_ms,
                raw_exit_price=bar.open,
                reason=pending_exit,
                commission_rate=commission_rate,
                slippage_bps=slippage_bps,
                commission_per_unit=commission_per_unit,
                commission_round_turn_per_lot=commission_round_turn_per_lot,
                amount_per_lot=amount_per_lot,
                spread_bps=spread_bps,
                financing_bps_per_day=financing_bps_per_day,
                market_impact_bps=market_impact_bps,
            )
            trades.append(trade)
            realized_pnl += trade.net_pnl
            position = None
            pending_exit = None

        if pending_entry is not None and pending_entry[0] <= index and position is None:
            signal_event = pending_entry[1]
            pending_entry = None
            if daily_loss_triggered or drawdown_triggered:
                blocked_entry_count += 1
            else:
                entry_price = _buy_fill(
                    bar.open,
                    slippage_bps,
                    spread_bps=spread_bps,
                    market_impact_bps=market_impact_bps,
                )
                atr_stop_price = entry_price - signal_event.atr * atr_stop_multiple
                stop_price = atr_stop_price
                stop_reason = "PROTECTIVE_STOP"
                if max_trade_loss_pct is not None:
                    entry_equity = initial_equity + realized_pnl
                    risk_budget = entry_equity * max_trade_loss_pct / 100.0
                    loss_stop_price = entry_price - risk_budget / amount
                    if loss_stop_price > stop_price:
                        stop_price = loss_stop_price
                        stop_reason = "MAX_TRADE_LOSS"
                position = _OpenPosition(
                    entry_time_ms=bar.time_ms,
                    entry_price=entry_price,
                    amount=amount,
                    stop_price=stop_price,
                    stop_reason=stop_reason,
                    entry_event=signal_event,
                )

        if position is not None and bar.low <= position.stop_price:
            raw_exit_price = bar.open if bar.open <= position.stop_price else position.stop_price
            stop_event = _stop_event(bar, index, indicators, position)
            events.append(stop_event)
            trade = _close_position(
                position,
                exit_time_ms=bar.time_ms,
                raw_exit_price=raw_exit_price,
                reason=position.stop_reason,
                commission_rate=commission_rate,
                slippage_bps=slippage_bps,
                commission_per_unit=commission_per_unit,
                commission_round_turn_per_lot=commission_round_turn_per_lot,
                amount_per_lot=amount_per_lot,
                spread_bps=spread_bps,
                financing_bps_per_day=financing_bps_per_day,
                market_impact_bps=market_impact_bps,
            )
            trades.append(trade)
            realized_pnl += trade.net_pnl
            if trade.exit_reason == "MAX_TRADE_LOSS":
                risk_limit_triggers["MAX_TRADE_LOSS"] += 1
            position = None

        if index < evaluation_end - 1:
            new_signal = _signal_at(
                bars,
                indicators,
                index=index,
                position_side=PositionSide.LONG if position is not None else None,
                atr_buffer=atr_buffer,
            )
            if new_signal is not None:
                signal_event = replace(
                    new_signal,
                    execution_time_ms=bars[index + 1].time_ms,
                )
                events.append(signal_event)
                if signal_event.signal is Signal.OPEN_BUY:
                    pending_entry = (index + 1, signal_event)
                elif signal_event.signal is Signal.CLOSE_BUY and pending_exit is None:
                    pending_exit = signal_event.reason

            if (
                position is not None
                and pending_exit is None
                and max_holding_hours is not None
                and bars[index + 1].time_ms - position.entry_time_ms
                >= max_holding_hours * 60 * 60 * 1000
            ):
                events.append(_holding_time_event(bars, index, indicators, position))
                pending_exit = "MAX_HOLDING_TIME"

        equity = _mark_equity(
            initial_equity,
            realized_pnl,
            position,
            _sell_fill(bar.close, spread_bps=spread_bps),
        )
        peak_equity = max(peak_equity, equity)
        if (
            max_daily_loss_pct is not None
            and not daily_loss_triggered
            and equity <= daily_start_equity * (1.0 - max_daily_loss_pct / 100.0)
        ):
            daily_loss_triggered = True
            risk_limit_triggers["MAX_DAILY_LOSS"] += 1
            if position is not None and index < evaluation_end - 1:
                events.append(_risk_exit_event(bars, index, indicators, position, "MAX_DAILY_LOSS"))
                pending_exit = "MAX_DAILY_LOSS"
        if (
            max_drawdown_pct is not None
            and not drawdown_triggered
            and equity <= peak_equity * (1.0 - max_drawdown_pct / 100.0)
        ):
            drawdown_triggered = True
            risk_limit_triggers["MAX_DRAWDOWN"] += 1
            if position is not None and index < evaluation_end - 1:
                events.append(_risk_exit_event(bars, index, indicators, position, "MAX_DRAWDOWN"))
                pending_exit = "MAX_DRAWDOWN"
        if index > evaluation_start_index:
            equity_curve.append(equity)

    if position is not None:
        trade = _close_position(
            position,
            exit_time_ms=bars[evaluation_end - 1].time_ms,
            raw_exit_price=bars[evaluation_end - 1].close,
            reason="MARK_TO_MARKET",
            commission_rate=commission_rate,
            slippage_bps=slippage_bps,
            commission_per_unit=commission_per_unit,
            commission_round_turn_per_lot=commission_round_turn_per_lot,
            amount_per_lot=amount_per_lot,
            spread_bps=spread_bps,
            financing_bps_per_day=financing_bps_per_day,
            market_impact_bps=market_impact_bps,
        )
        trades.append(trade)
        realized_pnl += trade.net_pnl
        equity_curve[-1] = initial_equity + realized_pnl

    benchmark_curve, benchmark_pnl = _buy_and_hold_curve(
        bars,
        amount=amount,
        initial_equity=initial_equity,
        commission_rate=commission_rate,
        slippage_bps=slippage_bps,
        commission_per_unit=commission_per_unit,
        commission_round_turn_per_lot=commission_round_turn_per_lot,
        amount_per_lot=amount_per_lot,
        spread_bps=spread_bps,
        financing_bps_per_day=financing_bps_per_day,
        market_impact_bps=market_impact_bps,
        start_index=evaluation_start_index,
        end_index=evaluation_end,
    )
    strategy_return = realized_pnl / initial_equity
    benchmark_return = benchmark_pnl / initial_equity
    opened = any(event.signal is Signal.OPEN_BUY for event in events)
    outcome = Outcome.SUCCESS if opened else Outcome.NO_SIGNAL
    message = (
        "SMA replay completed with transaction costs and next-bar execution."
        if opened
        else "No valid SMA bullish crossover occurred."
    )
    return SmaBacktestResult(
        outcome=outcome,
        events=events,
        trades=trades,
        final_equity=equity_curve[-1],
        buy_and_hold_equity=initial_equity + benchmark_pnl,
        strategy_return=strategy_return,
        buy_and_hold_return=benchmark_return,
        strategy_sharpe=_sharpe(equity_curve, annualization_factor=annualization_factor),
        buy_and_hold_sharpe=_sharpe(benchmark_curve, annualization_factor=annualization_factor),
        strategy_max_drawdown=_max_drawdown(equity_curve),
        buy_and_hold_max_drawdown=_max_drawdown(benchmark_curve),
        risk_limit_triggers=risk_limit_triggers,
        blocked_entry_count=blocked_entry_count,
        message=message,
    )


def synthetic_sma_fixture() -> list[Bar]:
    closes = (
        [100.0 - index * 0.2 for index in range(70)]
        + [86.0 + index * 0.55 for index in range(90)]
        + [135.0 - index * 0.65 for index in range(80)]
    )
    return [
        Bar(
            time_ms=index * 86_400_000,
            open=close,
            high=close + 0.25,
            low=close - 0.25,
            close=close,
        )
        for index, close in enumerate(closes)
    ]


def _signal_at(
    bars: list[Bar],
    indicators: SmaIndicators,
    *,
    index: int,
    position_side: PositionSide | None,
    atr_buffer: float,
) -> SmaSignalEvent | None:
    if index < 1:
        return None
    values = (
        float(indicators.fast[index - 1]),
        float(indicators.slow[index - 1]),
        float(indicators.atr[index - 1]),
        float(indicators.fast[index]),
        float(indicators.slow[index]),
        float(indicators.atr[index]),
    )
    if not all(isfinite(value) for value in values):
        return None
    previous_fast, previous_slow, previous_atr, current_fast, current_slow, current_atr = values
    if position_side is None and previous_fast <= previous_slow and current_fast > current_slow:
        return SmaSignalEvent(
            signal_time_ms=bars[index].time_ms,
            execution_time_ms=None,
            signal=Signal.OPEN_BUY,
            reason="BULLISH_SMA_CROSSOVER",
            sma_fast=current_fast,
            sma_slow=current_slow,
            atr=current_atr,
            close=bars[index].close,
        )
    if position_side is not PositionSide.LONG:
        return None
    previous_exit_level = previous_slow - atr_buffer * previous_atr
    current_exit_level = current_slow - atr_buffer * current_atr
    if previous_fast >= previous_exit_level and current_fast < current_exit_level:
        return SmaSignalEvent(
            signal_time_ms=bars[index].time_ms,
            execution_time_ms=None,
            signal=Signal.CLOSE_BUY,
            reason="BEARISH_SMA_CROSSOVER_WITH_ATR_BUFFER",
            sma_fast=current_fast,
            sma_slow=current_slow,
            atr=current_atr,
            close=bars[index].close,
        )
    return None


def _stop_event(
    bar: Bar, index: int, indicators: SmaIndicators, position: _OpenPosition
) -> SmaSignalEvent:
    values = (
        float(indicators.fast[index]),
        float(indicators.slow[index]),
        float(indicators.atr[index]),
    )
    fast = values[0] if isfinite(values[0]) else position.entry_event.sma_fast
    slow = values[1] if isfinite(values[1]) else position.entry_event.sma_slow
    atr = values[2] if isfinite(values[2]) else position.entry_event.atr
    return SmaSignalEvent(
        signal_time_ms=bar.time_ms,
        execution_time_ms=bar.time_ms,
        signal=Signal.CLOSE_BUY,
        reason=position.stop_reason,
        sma_fast=fast,
        sma_slow=slow,
        atr=atr,
        close=bar.close,
        stop_price=position.stop_price,
    )


def _risk_exit_event(
    bars: list[Bar],
    index: int,
    indicators: SmaIndicators,
    position: _OpenPosition,
    reason: str,
) -> SmaSignalEvent:
    values = (
        float(indicators.fast[index]),
        float(indicators.slow[index]),
        float(indicators.atr[index]),
    )
    fast = values[0] if isfinite(values[0]) else position.entry_event.sma_fast
    slow = values[1] if isfinite(values[1]) else position.entry_event.sma_slow
    atr = values[2] if isfinite(values[2]) else position.entry_event.atr
    return SmaSignalEvent(
        signal_time_ms=bars[index].time_ms,
        execution_time_ms=bars[index + 1].time_ms,
        signal=Signal.CLOSE_BUY,
        reason=reason,
        sma_fast=fast,
        sma_slow=slow,
        atr=atr,
        close=bars[index].close,
        stop_price=position.stop_price,
    )


def _holding_time_event(
    bars: list[Bar], index: int, indicators: SmaIndicators, position: _OpenPosition
) -> SmaSignalEvent:
    return _risk_exit_event(bars, index, indicators, position, "MAX_HOLDING_TIME")


def _mark_equity(
    initial_equity: float,
    realized_pnl: float,
    position: _OpenPosition | None,
    mark_price: float,
) -> float:
    equity = initial_equity + realized_pnl
    if position is not None:
        equity += (mark_price - position.entry_price) * position.amount
    return equity


## calcs complete trade
def _close_position(
    position: _OpenPosition,
    *,
    exit_time_ms: int,
    raw_exit_price: float,
    reason: str,
    commission_rate: float,
    slippage_bps: float,
    commission_per_unit: float,
    commission_round_turn_per_lot: float,
    amount_per_lot: float,
    spread_bps: float,
    financing_bps_per_day: float,
    market_impact_bps: float,
) -> SmaTrade:
    exit_price = _sell_fill(
        raw_exit_price,
        slippage_bps,
        spread_bps=spread_bps,
        market_impact_bps=market_impact_bps,
    )
    gross_pnl = (exit_price - position.entry_price) * position.amount
    commission = (
        commission_rate * position.amount * (position.entry_price + exit_price)
        + commission_per_unit * position.amount * 2.0
        + commission_round_turn_per_lot * position.amount / amount_per_lot
    )
    financing = _financing_cost(
        entry_time_ms=position.entry_time_ms,
        exit_time_ms=exit_time_ms,
        entry_price=position.entry_price,
        amount=position.amount,
        financing_bps_per_day=financing_bps_per_day,
    )
    return SmaTrade(
        entry_time_ms=position.entry_time_ms,
        exit_time_ms=exit_time_ms,
        entry_price=position.entry_price,
        exit_price=exit_price,
        amount=position.amount,
        gross_pnl=gross_pnl,
        commission=commission,
        financing=financing,
        net_pnl=gross_pnl - commission - financing,
        exit_reason=reason,
    )


def _buy_and_hold_curve(
    bars: list[Bar],
    *,
    amount: float,
    initial_equity: float,
    commission_rate: float,
    slippage_bps: float,
    commission_per_unit: float,
    commission_round_turn_per_lot: float,
    amount_per_lot: float,
    spread_bps: float,
    financing_bps_per_day: float,
    market_impact_bps: float,
    start_index: int = 0,
    end_index: int | None = None,
) -> tuple[list[float], float]:
    final_index = len(bars) if end_index is None else end_index
    period_bars = bars[start_index:final_index]
    entry_time_ms = period_bars[0].time_ms
    entry_price = _buy_fill(
        period_bars[0].open,
        slippage_bps,
        spread_bps=spread_bps,
        market_impact_bps=market_impact_bps,
    )
    round_turn_commission = commission_round_turn_per_lot * amount / amount_per_lot
    entry_commission = (
        commission_rate * amount * entry_price
        + commission_per_unit * amount
        + round_turn_commission / 2.0
    )
    exit_price = _sell_fill(
        period_bars[-1].close,
        slippage_bps,
        spread_bps=spread_bps,
        market_impact_bps=market_impact_bps,
    )
    exit_commission = (
        commission_rate * amount * exit_price
        + commission_per_unit * amount
        + round_turn_commission / 2.0
    )
    financing = _financing_cost(
        entry_time_ms=entry_time_ms,
        exit_time_ms=period_bars[-1].time_ms,
        entry_price=entry_price,
        amount=amount,
        financing_bps_per_day=financing_bps_per_day,
    )
    pnl = (exit_price - entry_price) * amount - entry_commission - exit_commission - financing
    curve = [initial_equity]
    for bar in period_bars[1:]:
        mark_price = _sell_fill(bar.close, spread_bps=spread_bps)
        carry = _financing_cost(
            entry_time_ms=entry_time_ms,
            exit_time_ms=bar.time_ms,
            entry_price=entry_price,
            amount=amount,
            financing_bps_per_day=financing_bps_per_day,
        )
        curve.append(
            initial_equity + (mark_price - entry_price) * amount - entry_commission - carry
        )
    curve[-1] = initial_equity + pnl
    return curve, pnl


def _buy_fill(
    price: float,
    slippage_bps: float = 0.0,
    *,
    spread_bps: float = 0.0,
    market_impact_bps: float = 0.0,
) -> float:
    return price * (1.0 + spread_bps / 20_000.0 + (slippage_bps + market_impact_bps) / 10_000.0)


def _sell_fill(
    price: float,
    slippage_bps: float = 0.0,
    *,
    spread_bps: float = 0.0,
    market_impact_bps: float = 0.0,
) -> float:
    return price * (1.0 - spread_bps / 20_000.0 - (slippage_bps + market_impact_bps) / 10_000.0)


## holding cost estimte
def _financing_cost(
    *,
    entry_time_ms: int,
    exit_time_ms: int,
    entry_price: float,
    amount: float,
    financing_bps_per_day: float,
) -> float:
    held_days = max(0.0, (exit_time_ms - entry_time_ms) / 86_400_000.0)
    return financing_bps_per_day / 10_000.0 * entry_price * amount * held_days


def _sharpe(equity_curve: list[float], *, annualization_factor: float) -> float | None:
    values = np.asarray(equity_curve, dtype=np.float64)
    if len(values) < 3 or not np.all(np.isfinite(values)) or np.any(values[:-1] <= 0):
        return None
    returns = np.diff(values) / values[:-1]
    deviation = float(np.std(returns, ddof=1))
    if deviation == 0.0:
        return None
    return float(np.mean(returns) / deviation * sqrt(annualization_factor))


def _max_drawdown(equity_curve: list[float]) -> float:
    values = np.asarray(equity_curve, dtype=np.float64)
    peaks = np.maximum.accumulate(values)
    drawdowns = (values - peaks) / peaks
    return float(max(0.0, -float(np.min(drawdowns))))


def _validate_evaluation_bounds(
    *,
    bar_count: int,
    evaluation_start_index: int,
    evaluation_end_index: int,
    required_count: int,
) -> None:
    if evaluation_start_index < 0:
        raise ValueError("evaluation_start_index must not be negative.")
    if evaluation_end_index > bar_count:
        raise ValueError("evaluation_end_index must not exceed the number of bars.")
    if evaluation_start_index >= evaluation_end_index - 1:
        raise ValueError("Evaluation must contain at least two bars after the start index.")
    if evaluation_start_index == 0 and evaluation_end_index < required_count:
        raise ValueError("evaluation_end_index must include enough bars for SMA/ATR warm-up.")
    if evaluation_start_index > 0 and evaluation_start_index < required_count - 1:
        raise ValueError(
            f"evaluation_start_index must have at least {required_count - 1} prior bars "
            "for SMA/ATR warm-up."
        )


def _empty_result(outcome: Outcome, message: str) -> SmaBacktestResult:
    return SmaBacktestResult(
        outcome=outcome,
        events=[],
        trades=[],
        final_equity=0.0,
        buy_and_hold_equity=0.0,
        strategy_return=0.0,
        buy_and_hold_return=0.0,
        strategy_sharpe=None,
        buy_and_hold_sharpe=None,
        strategy_max_drawdown=0.0,
        buy_and_hold_max_drawdown=0.0,
        risk_limit_triggers={},
        blocked_entry_count=0,
        message=message,
    )


def _validate_parameters(
    *,
    fast_period: int,
    slow_period: int,
    atr_period: int,
    atr_buffer: float,
    atr_stop_multiple: float,
    amount: float,
    commission_rate: float,
    slippage_bps: float,
    commission_per_unit: float = 0.0,
    commission_round_turn_per_lot: float = 0.0,
    amount_per_lot: float = 1.0,
    spread_bps: float = 0.0,
    financing_bps_per_day: float = 0.0,
    market_impact_bps: float = 0.0,
    max_holding_hours: float | None = None,
    max_trade_loss_pct: float | None = None,
    max_daily_loss_pct: float | None = None,
    max_drawdown_pct: float | None = None,
) -> None:
    if fast_period <= 0 or slow_period <= 0 or atr_period <= 0:
        raise ValueError("SMA and ATR periods must be greater than zero.")
    if fast_period >= slow_period:
        raise ValueError("fast_period must be below slow_period.")
    if not isfinite(atr_buffer) or atr_buffer < 0:
        raise ValueError("atr_buffer must be finite and not negative.")
    if not isfinite(atr_stop_multiple) or atr_stop_multiple < 0:
        raise ValueError("atr_stop_multiple must be finite and not negative.")
    if not isfinite(amount) or amount <= 0:
        raise ValueError("amount must be finite and greater than zero.")
    if max_holding_hours is not None and (
        not isfinite(max_holding_hours) or max_holding_hours <= 0
    ):
        raise ValueError("max_holding_hours must be finite and greater than zero when set.")
    for name, value in (
        ("max_trade_loss_pct", max_trade_loss_pct),
        ("max_daily_loss_pct", max_daily_loss_pct),
        ("max_drawdown_pct", max_drawdown_pct),
    ):
        if value is not None and (not isfinite(value) or value <= 0 or value > 100):
            raise ValueError(f"{name} must be between zero and 100 when set.")
    if not isfinite(commission_rate) or commission_rate < 0:
        raise ValueError("commission_rate must be finite and not negative.")
    if not isfinite(slippage_bps) or not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage_bps must be finite and between zero and 10000.")
    if not isfinite(commission_per_unit) or commission_per_unit < 0:
        raise ValueError("commission_per_unit must be finite and not negative.")
    if not isfinite(commission_round_turn_per_lot) or commission_round_turn_per_lot < 0:
        raise ValueError("commission_round_turn_per_lot must be finite and not negative.")
    if not isfinite(amount_per_lot) or amount_per_lot <= 0:
        raise ValueError("amount_per_lot must be finite and greater than zero.")
    for name, value in (
        ("spread_bps", spread_bps),
        ("financing_bps_per_day", financing_bps_per_day),
        ("market_impact_bps", market_impact_bps),
    ):
        if not isfinite(value) or not 0 <= value < 10_000:
            raise ValueError(f"{name} must be finite and between zero and 10000.")


def _validate_bars(bars: list[Bar]) -> None:
    previous_time = -1
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close)
        if not all(isfinite(value) and value > 0 for value in values):
            raise ValueError("Bars must contain finite positive OHLC values.")
        if bar.time_ms <= previous_time:
            raise ValueError("Bars must be ordered by strictly increasing time.")
        previous_time = bar.time_ms
