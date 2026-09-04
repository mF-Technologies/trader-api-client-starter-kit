import json
from pathlib import Path

import pytest

from trader_api_examples.cli import build_parser, main


@pytest.mark.parametrize(
    "command",
    [
        "account-inspector",
        "contract-calculator",
        "market-data-monitor",
        "order-lifecycle-checker",
        "rsi-algo-demo",
        "algo-runner",
        "recover",
    ],
)
def test_cli_exposes_expected_commands(command: str) -> None:
    parser = build_parser()
    args = parser.parse_args([command, "--config", "config.local.yaml"])

    assert args.command == command


def test_algo_runner_defaults_to_observe_mode() -> None:
    args = build_parser().parse_args(["algo-runner", "--config", "config.local.yaml"])

    assert args.mode == "live-observe"
    assert args.execute is False


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
