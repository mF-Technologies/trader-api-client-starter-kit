import json
from pathlib import Path

import pytest

from trader_api_examples.cli import build_parser, main
from trader_api_examples.commands import load_ema_replay_fixture, load_sma_replay_fixture


@pytest.mark.parametrize(
    "command",
    [
        "account-inspector",
        "contract-calculator",
        "execution-cost-audit",
        "sma-data-export",
        "market-data-monitor",
        "order-lifecycle-checker",
        "rsi-algo-demo",
        "sma-algo-demo",
        "ema-algo-demo",
        "recover",
    ],
)
def test_cli_exposes_expected_commands(command: str) -> None:
    parser = build_parser()
    command_args = [command, "--config", "config.local.yaml"]
    if command == "sma-data-export":
        command_args.extend(["--output-file", "bars.json"])
    args = parser.parse_args(command_args)

    assert args.command == command


def test_replay_runs_without_api_key_and_writes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADER_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("environment: demo\n", encoding="utf-8")

    exit_code = main(
        ["rsi-algo-demo", "--config", str(config), "--mode", "replay", "--output", "json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["outcome"] == "SUCCESS"
    assert [event["signal"] for event in payload["data"]["events"]] == [
        "OPEN_BUY",
        "CLOSE_BUY",
    ]


def test_execution_cost_audit_defaults_to_read_only_snapshot() -> None:
    parser = build_parser()

    args = parser.parse_args(["execution-cost-audit", "--config", "config.local.yaml"])

    assert args.command == "execution-cost-audit"
    assert args.mode == "snapshot"
    assert args.execute is False
    assert args.amount is None
    assert args.hold_seconds == 2.0
    assert args.samples == 1


def test_sma_replay_exposes_round_turn_commission_parameters() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "sma-algo-demo",
            "--config",
            "config.local.yaml",
            "--mode",
            "replay",
            "--commission-round-turn-per-lot",
            "7",
            "--amount-per-lot",
            "100",
        ]
    )

    assert args.commission_round_turn_per_lot == 7.0
    assert args.amount_per_lot == 100.0


def test_ema_replay_exposes_ema_parameters() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "ema-algo-demo",
            "--config",
            "config.local.yaml",
            "--mode",
            "replay",
            "--buffer",
            "0.5",
            "--stop-multiple",
            "3",
        ]
    )

    assert args.command == "ema-algo-demo"
    assert args.buffer == 0.5
    assert args.stop_multiple == 3.0


def test_sma_replay_runs_without_api_key_and_includes_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADER_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("environment: demo\n", encoding="utf-8")

    exit_code = main(
        ["sma-algo-demo", "--config", str(config), "--mode", "replay", "--output", "json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["outcome"] == "SUCCESS"
    assert payload["data"]["period_type"] == 3
    assert "buy_and_hold_return" in payload["data"]["metrics"]


def test_ema_replay_runs_without_api_key_and_includes_ema_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADER_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("environment: demo\n", encoding="utf-8")

    exit_code = main(
        ["ema-algo-demo", "--config", str(config), "--mode", "replay", "--output", "json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["outcome"] == "SUCCESS"
    assert payload["data"]["period_type"] == 3
    assert payload["data"]["events"][0]["reason"] == "BULLISH_EMA_CROSSOVER"
    assert "buy_and_hold_return" in payload["data"]["metrics"]


def test_sma_replay_reports_evaluation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADER_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text("environment: demo\n", encoding="utf-8")

    exit_code = main(
        [
            "sma-algo-demo",
            "--config",
            str(config),
            "--mode",
            "replay",
            "--evaluation-start-index",
            "70",
            "--evaluation-end-index",
            "180",
            "--output",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["evaluation"] == {
        "start_index": 70,
        "end_index": 180,
        "warmup_bar_count": 70,
        "start_time_ms": 70 * 86_400_000,
        "end_time_ms": 179 * 86_400_000,
    }


def test_sma_replay_loads_tab_delimited_market_csv(tmp_path: Path) -> None:
    fixture = tmp_path / "xauusd.csv"
    fixture.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n"
        "2007.01.01\t03:00:00\t636.45\t638.05\t636.30\t636.85\n",
        encoding="utf-8",
    )

    bars = load_sma_replay_fixture(fixture)

    assert len(bars) == 1
    assert bars[0].time_ms == 1167620400000
    assert bars[0].open == 636.45
    assert bars[0].close == 636.85


def test_ema_replay_loads_tab_delimited_market_csv(tmp_path: Path) -> None:
    fixture = tmp_path / "xauusd.csv"
    fixture.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n"
        "2007.01.01\t03:00:00\t636.45\t638.05\t636.30\t636.85\n",
        encoding="utf-8",
    )

    bars = load_ema_replay_fixture(fixture)

    assert len(bars) == 1
    assert bars[0].time_ms == 1167620400000
