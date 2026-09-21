from dataclasses import replace
from typing import Any, cast

import pytest

from trader_api_examples.ai_risk import AccountContext, RiskDecision
from trader_api_examples.algo import Outcome
from trader_api_examples.api import Bar
from trader_api_examples.sma_algo import (
    SmaSignalEvent,
    calculate_sma_indicators,
    evaluate_sma_replay,
    latest_sma_signal,
)
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


def test_sma_replay_uses_next_bar_execution_and_transaction_costs() -> None:
    bars = trend_reversal_bars()
    result = evaluate_sma_replay(
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
    assert open_event.execution_time_ms == bars[open_index + 1].time_ms
    assert result.trades[0].entry_time_ms == open_event.execution_time_ms
    assert result.trades[0].commission > 0
    assert result.buy_and_hold_return != result.strategy_return


def test_sma_replay_models_explicit_cost_components() -> None:
    bars = trend_reversal_bars()
    baseline = evaluate_sma_replay(bars, amount=100.0, atr_stop_multiple=100.0)
    costed = evaluate_sma_replay(
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
    assert costed.buy_and_hold_return < baseline.buy_and_hold_return


class StubRiskAgent:
    def __init__(
        self, decision: RiskDecision | None = None, error: Exception | None = None
    ) -> None:
        self.decision = decision
        self.error = error
        self.contexts: list[AccountContext] = []

    def decide(self, context: AccountContext) -> RiskDecision:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


def test_sma_replay_ai_reduce_only_changes_entry_size() -> None:
    agent = StubRiskAgent(RiskDecision("REDUCE", "Elevated volatility.", 0.8))

    result = evaluate_sma_replay(
        trend_reversal_bars(),
        amount=100.0,
        atr_stop_multiple=100.0,
        risk_agent=agent,
        ai_reduce_size_multiplier=0.5,
    )

    assert result.ai_risk_enabled is True
    assert result.ai_risk_failures == 0
    assert result.ai_risk_decisions[0]["action"] == "REDUCE"
    assert result.trades[0].amount == 50.0
    assert agent.contexts[0].market_data_fresh is True


def test_sma_replay_ai_pause_blocks_entry() -> None:
    agent = StubRiskAgent(RiskDecision("PAUSE", "Drawdown is too high.", 0.9))

    result = evaluate_sma_replay(
        trend_reversal_bars(),
        amount=100.0,
        atr_stop_multiple=100.0,
        risk_agent=agent,
    )

    assert result.ai_risk_decisions[0]["action"] == "PAUSE"
    assert result.trades == []


def test_sma_replay_ai_failure_fails_closed() -> None:
    agent = StubRiskAgent(error=RuntimeError("agent unavailable"))

    result = evaluate_sma_replay(
        trend_reversal_bars(),
        amount=100.0,
        atr_stop_multiple=100.0,
        risk_agent=agent,
    )

    assert result.ai_risk_failures == 1
    assert result.ai_risk_decisions[0]["action"] == "PAUSE"
    assert result.ai_risk_decisions[0]["failed"] is True
    assert result.trades == []


def test_sma_replay_applies_round_turn_commission_per_lot_once() -> None:
    result = evaluate_sma_replay(
        trend_reversal_bars(),
        amount=2.0,
        amount_per_lot=2.0,
        commission_round_turn_per_lot=7.0,
        atr_stop_multiple=100.0,
    )

    trade = result.trades[0]
    assert trade.commission == pytest.approx(7.0)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.commission - trade.financing)


@pytest.mark.parametrize(
    "parameter",
    [
        "commission_per_unit",
        "commission_round_turn_per_lot",
        "spread_bps",
        "financing_bps_per_day",
        "market_impact_bps",
    ],
)
def test_sma_replay_rejects_negative_cost_assumptions(parameter: str) -> None:
    with pytest.raises(ValueError, match=parameter):
        evaluate_sma_replay(trend_reversal_bars(), **cast(Any, {parameter: -0.01}))


def test_sma_replay_uses_warmup_bars_without_scoring_them() -> None:
    bars = trend_reversal_bars()
    result = evaluate_sma_replay(
        bars,
        atr_stop_multiple=100.0,
        evaluation_start_index=70,
        evaluation_end_index=180,
    )

    open_event = next(event for event in result.events if event.signal is Signal.OPEN_BUY)
    assert open_event.signal_time_ms == bars[86].time_ms
    assert all(event.signal_time_ms >= bars[70].time_ms for event in result.events)
    assert all(trade.entry_time_ms >= bars[70].time_ms for trade in result.trades)
    assert all(trade.exit_time_ms <= bars[179].time_ms for trade in result.trades)
    assert result.buy_and_hold_return == pytest.approx(
        (bars[179].close - bars[70].open) / bars[70].open
    )


def test_sma_replay_starts_holdout_flat() -> None:
    bars = trend_reversal_bars()
    result = evaluate_sma_replay(
        bars,
        atr_stop_multiple=100.0,
        evaluation_start_index=100,
        evaluation_end_index=180,
    )

    assert result.events == []
    assert result.trades == []
    assert result.strategy_return == 0.0


def test_sma_replay_requires_warmup_before_nonzero_holdout_start() -> None:
    with pytest.raises(ValueError, match="prior bars"):
        evaluate_sma_replay(trend_reversal_bars(), evaluation_start_index=49)


def test_sma_replay_annualizes_sharpe_for_selected_bar_interval() -> None:
    bars = trend_reversal_bars()
    daily = evaluate_sma_replay(bars, atr_stop_multiple=100.0, annualization_factor=252.0)
    hourly = evaluate_sma_replay(bars, atr_stop_multiple=100.0, annualization_factor=252.0 * 24.0)

    assert daily.strategy_sharpe is not None
    assert hourly.strategy_sharpe == pytest.approx(daily.strategy_sharpe * (24.0**0.5))


def test_atr_buffer_delays_bearish_exit() -> None:
    bars = trend_reversal_bars()
    no_buffer = evaluate_sma_replay(bars, atr_buffer=0.0, atr_stop_multiple=100.0)
    buffered = evaluate_sma_replay(bars, atr_buffer=0.5, atr_stop_multiple=100.0)

    no_buffer_exit = next(
        event
        for event in no_buffer.events
        if event.reason == "BEARISH_SMA_CROSSOVER_WITH_ATR_BUFFER"
    )
    buffered_exit = next(
        event
        for event in buffered.events
        if event.reason == "BEARISH_SMA_CROSSOVER_WITH_ATR_BUFFER"
    )
    assert buffered_exit.signal_time_ms > no_buffer_exit.signal_time_ms


def test_protective_stop_executes_on_entry_bar() -> None:
    bars = trend_reversal_bars()
    indicators = calculate_sma_indicators(bars, fast_period=20, slow_period=50, atr_period=14)
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

    result = evaluate_sma_replay(bars, atr_stop_multiple=1.0)

    stop_trade = next(trade for trade in result.trades if trade.exit_reason == "PROTECTIVE_STOP")
    assert stop_trade.entry_time_ms == stop_trade.exit_time_ms
    assert any(event.reason == "PROTECTIVE_STOP" for event in result.events)


def test_replay_allows_zero_atr_stop_multiple_as_entry_price_stop() -> None:
    result = evaluate_sma_replay(trend_reversal_bars(), atr_stop_multiple=0.0)

    assert any(trade.exit_reason == "PROTECTIVE_STOP" for trade in result.trades)


def test_replay_exits_at_maximum_holding_time_on_next_bar() -> None:
    result = evaluate_sma_replay(
        trend_reversal_bars(),
        atr_stop_multiple=100.0,
        max_holding_hours=72.0,
    )

    timed_trade = next(trade for trade in result.trades if trade.exit_reason == "MAX_HOLDING_TIME")
    assert timed_trade.exit_time_ms - timed_trade.entry_time_ms == 72 * 60 * 60 * 1000


def test_replay_rejects_nonpositive_maximum_holding_time() -> None:
    with pytest.raises(ValueError, match="max_holding_hours"):
        evaluate_sma_replay(trend_reversal_bars(), max_holding_hours=0.0)


def test_replay_applies_per_trade_loss_limit_to_protective_stop() -> None:
    result = evaluate_sma_replay(
        trend_reversal_bars(),
        amount=100.0,
        atr_buffer=100.0,
        atr_stop_multiple=100.0,
        max_trade_loss_pct=1.0,
    )

    assert result.risk_limit_triggers["MAX_TRADE_LOSS"] > 0
    assert any(trade.exit_reason == "MAX_TRADE_LOSS" for trade in result.trades)


def test_replay_applies_daily_loss_limit() -> None:
    bars = [
        replace(bar, time_ms=index * 3_600_000) for index, bar in enumerate(trend_reversal_bars())
    ]
    result = evaluate_sma_replay(
        bars,
        amount=100.0,
        atr_stop_multiple=100.0,
        max_daily_loss_pct=1.0,
    )

    assert result.risk_limit_triggers["MAX_DAILY_LOSS"] > 0
    assert any(trade.exit_reason == "MAX_DAILY_LOSS" for trade in result.trades)


def test_replay_applies_drawdown_limit() -> None:
    bars = [
        replace(bar, time_ms=index * 3_600_000) for index, bar in enumerate(trend_reversal_bars())
    ]
    result = evaluate_sma_replay(
        bars,
        amount=100.0,
        atr_stop_multiple=100.0,
        max_drawdown_pct=1.0,
    )

    assert result.risk_limit_triggers["MAX_DRAWDOWN"] > 0
    assert any(trade.exit_reason == "MAX_DRAWDOWN" for trade in result.trades)


def test_latest_signal_requires_valid_history_and_is_long_only() -> None:
    insufficient = bars_from_closes([100.0 + index for index in range(50)])
    assert latest_sma_signal(insufficient, position_side=None) is None

    bars = trend_reversal_bars()
    open_event = next_sma_signal(bars, None)
    close_event = next_sma_signal(bars, PositionSide.LONG)
    assert open_event is None or open_event.signal is Signal.OPEN_BUY
    assert close_event is None or close_event.signal is Signal.CLOSE_BUY


def test_replay_reports_inconclusive_without_enough_daily_bars() -> None:
    result = evaluate_sma_replay(bars_from_closes([100.0] * 50))

    assert result.outcome is Outcome.INCONCLUSIVE
    assert result.events == []
    assert result.trades == []


def next_sma_signal(bars: list[Bar], position_side: PositionSide | None) -> SmaSignalEvent | None:
    for end in range(52, len(bars) + 1):
        signal = latest_sma_signal(bars[:end], position_side=position_side)
        if signal is not None:
            return signal
    return None
