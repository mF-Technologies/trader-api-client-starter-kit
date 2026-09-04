from trader_api_examples.algo import (
    Outcome,
    evaluate_latest_strategy,
    evaluate_replay,
    latest_signal,
    latest_strategy_signal,
)
from trader_api_examples.api import Bar
from trader_api_examples.config import StrategyConfig
from trader_api_examples.strategy import PositionSide, Signal


def bars_from_closes(closes: list[float]) -> list[Bar]:
    return [
        Bar(time_ms=index * 60_000, open=value, high=value, low=value, close=value)
        for index, value in enumerate(closes)
    ]


def test_replay_completes_one_rsi_round_trip() -> None:
    closes = [100 - index for index in range(20)] + list(range(81, 91))

    result = evaluate_replay(bars_from_closes(closes), rsi_period=14)

    assert result.outcome is Outcome.SUCCESS
    assert [event.signal.value for event in result.events] == ["OPEN_BUY", "CLOSE_BUY"]
    assert result.events[0].rsi == 32.57
    assert result.events[1].rsi == 50.53


def test_replay_returns_no_signal_without_relaxing_thresholds() -> None:
    closes = [100 + (index % 2) * 0.1 for index in range(40)]

    result = evaluate_replay(bars_from_closes(closes), rsi_period=14)

    assert result.outcome is Outcome.NO_SIGNAL
    assert result.events == []


def test_latest_signal_uses_latest_crossing_and_owned_position_state() -> None:
    closes = [100 - index for index in range(20)] + list(range(81, 91))

    open_event = latest_signal(bars_from_closes(closes[:26]), rsi_period=14, position_side=None)
    assert open_event is not None
    assert open_event.signal is Signal.OPEN_BUY

    close_event = latest_signal(
        bars_from_closes(closes), rsi_period=14, position_side=PositionSide.LONG
    )
    assert close_event is not None
    assert close_event.signal is Signal.CLOSE_BUY


def test_latest_strategy_signal_supports_ema_cross() -> None:
    bars = bars_from_closes([10, 10, 10, 10, 10, 10, 10, 10, 1, 20])

    event = latest_strategy_signal(
        bars,
        strategy=StrategyConfig(name="ema_cross", fast_period=2, slow_period=4),
        position_side=None,
    )

    assert event is not None
    assert event.signal is Signal.OPEN_BUY
    assert event.strategy == "ema_cross"


def test_latest_strategy_signal_supports_macd() -> None:
    bars = bars_from_closes([10, 10, 10, 10, 10, 10, 10, 10, 1, 20])

    event = latest_strategy_signal(
        bars,
        strategy=StrategyConfig(name="macd", fast_period=2, slow_period=4, signal_period=2),
        position_side=None,
    )

    assert event is not None
    assert event.signal is Signal.OPEN_BUY
    assert event.strategy == "macd"


def test_latest_strategy_signal_returns_none_when_history_is_incomplete() -> None:
    event = latest_strategy_signal(
        bars_from_closes([1, 2, 3]),
        strategy=StrategyConfig(name="ema_cross", fast_period=2, slow_period=4),
        position_side=None,
    )

    assert event is None


def test_strategy_evaluation_exposes_indicator_values_without_a_signal() -> None:
    evaluation = evaluate_latest_strategy(
        bars_from_closes([100 + index * 0.01 for index in range(40)]),
        strategy=StrategyConfig(),
        position_side=None,
    )

    assert evaluation.event is None
    assert "rsi" in evaluation.indicators
