from trader_api_examples.strategy import PositionSide, Signal, signal_from_rsi_crossing


def test_rsi_crossing_up_out_of_oversold_opens_buy() -> None:
    assert signal_from_rsi_crossing(29.0, 31.0, None) is Signal.OPEN_BUY


def test_rsi_crossing_down_out_of_overbought_opens_sell() -> None:
    assert signal_from_rsi_crossing(71.0, 69.0, None) is Signal.OPEN_SELL


def test_remaining_inside_threshold_does_not_repeat_signal() -> None:
    assert signal_from_rsi_crossing(28.0, 27.0, None) is Signal.NONE
    assert signal_from_rsi_crossing(72.0, 73.0, None) is Signal.NONE


def test_position_exits_only_when_rsi_crosses_neutral() -> None:
    assert signal_from_rsi_crossing(49.0, 51.0, PositionSide.LONG) is Signal.CLOSE_BUY
    assert signal_from_rsi_crossing(51.0, 49.0, PositionSide.SHORT) is Signal.CLOSE_SELL
