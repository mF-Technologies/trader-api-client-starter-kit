from dataclasses import replace

import numpy as np

from trader_api_examples.algo import Outcome
from trader_api_examples.api import Bar
from trader_api_examples.ema_algo import (
    calculate_ema_indicators,
    evaluate_ema_replay,
    latest_ema_signal,
)
from trader_api_examples.sma_algo import calculate_sma_indicators
from trader_api_examples.strategy import PositionSide, Signal


def bars_from_closes(closes: list[float]) -> list[Bar]:
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


def trend_reversal_bars() -> list[Bar]:
    closes = (
        [100.0 - index * 0.2 for index in range(70)]
        + [86.0 + index * 0.55 for index in range(90)]
        + [135.0 - index * 0.65 for index in range(80)]
    )
    return bars_from_closes(closes)


def test_ema_indicators_use_exponential_averages() -> None:
    bars = trend_reversal_bars()

    ema = calculate_ema_indicators(bars, fast_period=20, slow_period=50, atr_period=14)
    sma = calculate_sma_indicators(bars, fast_period=20, slow_period=50, atr_period=14)

    assert np.all(np.isfinite(ema.fast[50:]))
    assert np.all(np.isfinite(ema.slow[50:]))
    assert not np.allclose(ema.fast[50:], sma.fast[50:])


def test_ema_replay_uses_next_bar_execution_and_transaction_costs() -> None:
    bars = trend_reversal_bars()
    result = evaluate_ema_replay(
        bars,
        amount=100.0,
        atr_stop_multiple=100.0,
        commission_rate=0.001,
        slippage_bps=10.0,
    )

    assert result.outcome is Outcome.SUCCESS
    open_event = next(event for event in result.events if event.signal is Signal.OPEN_BUY)
    open_index = next(
        index for index, bar in enumerate(bars) if bar.time_ms == open_event.signal_time_ms
    )
    assert open_event.reason == "BULLISH_EMA_CROSSOVER"
    assert open_event.execution_time_ms == bars[open_index + 1].time_ms
    assert result.trades[0].entry_time_ms == open_event.execution_time_ms
    assert result.trades[0].commission > 0


def test_ema_replay_models_explicit_cost_components() -> None:
    bars = trend_reversal_bars()
    baseline = evaluate_ema_replay(bars, amount=100.0, atr_stop_multiple=100.0)
    costed = evaluate_ema_replay(
        bars,
        amount=100.0,
        atr_stop_multiple=100.0,
        commission_per_unit=0.05,
        spread_bps=2.65,
        slippage_bps=0.11,
        financing_bps_per_day=5.0,
        market_impact_bps=2.0,
    )

    assert costed.trades
    assert costed.trades[0].commission >= 10.0
    assert costed.trades[0].financing > 0.0
    assert costed.strategy_return < baseline.strategy_return


def test_ema_latest_signal_is_long_only_and_needs_valid_history() -> None:
    insufficient = bars_from_closes([100.0 + index for index in range(50)])
    assert latest_ema_signal(insufficient, position_side=None) is None

    bars = trend_reversal_bars()
    open_event = next(
        (
            latest_ema_signal(bars[:end], position_side=None)
            for end in range(52, len(bars) + 1)
            if latest_ema_signal(bars[:end], position_side=None) is not None
        ),
        None,
    )
    assert open_event is not None
    assert open_event.signal is Signal.OPEN_BUY
    assert latest_ema_signal(bars, position_side=PositionSide.SHORT) is None


def test_ema_atr_buffer_delays_bearish_exit() -> None:
    bars = trend_reversal_bars()
    no_buffer = evaluate_ema_replay(bars, atr_buffer=0.0, atr_stop_multiple=100.0)
    buffered = evaluate_ema_replay(bars, atr_buffer=0.5, atr_stop_multiple=100.0)

    no_buffer_exit = next(
        event
        for event in no_buffer.events
        if event.reason == "BEARISH_EMA_CROSSOVER_WITH_ATR_BUFFER"
    )
    buffered_exit = next(
        event
        for event in buffered.events
        if event.reason == "BEARISH_EMA_CROSSOVER_WITH_ATR_BUFFER"
    )
    assert buffered_exit.signal_time_ms > no_buffer_exit.signal_time_ms


def test_ema_replay_returns_inconclusive_without_enough_bars() -> None:
    result = evaluate_ema_replay(bars_from_closes([100.0] * 50))

    assert result.outcome is Outcome.INCONCLUSIVE
    assert result.events == []
    assert result.trades == []


def test_ema_replay_applies_holding_limit() -> None:
    result = evaluate_ema_replay(
        trend_reversal_bars(),
        atr_stop_multiple=100.0,
        max_holding_hours=72.0,
    )

    timed_trade = next(trade for trade in result.trades if trade.exit_reason == "MAX_HOLDING_TIME")
    assert timed_trade.exit_time_ms - timed_trade.entry_time_ms == 72 * 60 * 60 * 1000


def test_ema_replay_protective_stop_executes_on_entry_bar() -> None:
    bars = trend_reversal_bars()
    indicators = calculate_ema_indicators(bars, fast_period=20, slow_period=50, atr_period=14)
    bullish_index = next(
        index
        for index in range(1, len(bars) - 1)
        if (
            indicators.fast[index - 1] <= indicators.slow[index - 1]
            and indicators.fast[index] > indicators.slow[index]
        )
    )
    entry_bar = bars[bullish_index + 1]
    stop_price = entry_bar.open - float(indicators.atr[bullish_index])
    bars[bullish_index + 1] = replace(entry_bar, low=max(0.01, stop_price - 1.0))

    result = evaluate_ema_replay(bars, atr_stop_multiple=1.0)

    stop_trade = next(trade for trade in result.trades if trade.exit_reason == "PROTECTIVE_STOP")
    assert stop_trade.entry_time_ms == stop_trade.exit_time_ms
