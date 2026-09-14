from pathlib import Path

import pytest

from trader_api_examples.config import (
    AppConfig,
    ConfigError,
    StrategyConfig,
    TradingConfig,
    load_config,
)


def test_default_research_contract_is_llg() -> None:
    assert TradingConfig().contract == "LLG"
    assert TradingConfig().max_total_open_positions == 1
    assert TradingConfig().amount_per_lot == 1.0
    assert TradingConfig().max_runtime_seconds == 600
    assert TradingConfig().max_holding_hours == 120.0
    assert TradingConfig().max_trade_loss_pct == 1.0
    assert TradingConfig().max_daily_loss_pct == 2.0
    assert TradingConfig().max_drawdown_pct == 10.0


def test_ema_defaults_follow_sma_timeframe_and_periods() -> None:
    config = AppConfig(environment="demo", strategy=StrategyConfig())

    assert config.strategy.ema_period_type is None
    assert config.strategy.ema_fast_period == 20
    assert config.strategy.ema_slow_period == 50
    assert config.strategy.ema_exit_buffer_atr == 0.0


def test_load_config_rejects_nonpositive_holding_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\ntrading:\n  max_holding_hours: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="max_holding_hours"):
        load_config(config_path)


def test_load_config_rejects_invalid_total_position_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\ntrading:\n  max_total_open_positions: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="max_total_open_positions"):
        load_config(config_path)


def test_load_config_rejects_invalid_live_risk_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\ntrading:\n  max_drawdown_pct: 101\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        load_config(config_path)


def test_load_config_rejects_invalid_round_turn_commission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\nstrategy:\n  commission_round_turn_per_lot: -1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="commission_round_turn_per_lot"):
        load_config(config_path)


def test_load_config_keeps_strategy_settings_and_resolves_secrets_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        """
environment: demo
endpoints:
  web_proxy_url: https://webproxy.example
  fxserver_rest_url: https://fxserver.example
  chart_server_url: https://chart.example
trading:
  contract: LLG
  amount: 1000
  max_runtime_seconds: 600
strategy:
  period_type: 1
  rsi_period: 14
  oversold: 30
  overbought: 70
  exit_level: 50
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "secret-api-key")  # pragma: allowlist secret

    config = load_config(config_path)

    assert config.environment == "demo"
    assert config.trading.contract == "LLG"
    assert config.trading.amount == 1000
    assert config.secrets.api_key == "secret-api-key"  # pragma: allowlist secret


def test_load_config_accepts_market_data_session_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        """
environment: demo
trading:
  market_data_daily_break_start_utc: "23:00"
  market_data_daily_break_end_utc: "01:00"
  market_data_closed_dates_utc:
    - 2026-12-25
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "secret-api-key")  # pragma: allowlist secret

    config = load_config(config_path)

    assert config.trading.market_data_daily_break_start_utc == "23:00"
    assert config.trading.market_data_daily_break_end_utc == "01:00"
    assert config.trading.market_data_closed_dates_utc == ("2026-12-25",)


def test_load_config_reads_dotenv_local_next_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("environment: demo\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "\n".join(
            (
                "TRADER_API_KEY=local-api-key",  # pragma: allowlist secret
                "TRADER_API_USERNAME=local-user",
                "TRADER_API_TRADE_KEY=local-trade-key",  # pragma: allowlist secret
                "TRADER_API_ENABLE_LIVE_TRADING=true",
            )
        ),
        encoding="utf-8",
    )
    for name in (
        "TRADER_API_KEY",
        "TRADER_API_USERNAME",
        "TRADER_API_TRADE_KEY",
        "TRADER_API_ENABLE_LIVE_TRADING",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config(config_path)

    assert config.secrets.api_key == "local-api-key"  # pragma: allowlist secret
    assert config.secrets.username == "local-user"
    assert config.secrets.trade_key == "local-trade-key"  # pragma: allowlist secret
    assert config.live_trading_enabled is True


def test_environment_variables_override_dotenv_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text("environment: demo\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "TRADER_API_KEY=local-api-key\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "environment-api-key")  # pragma: allowlist secret

    config = load_config(config_path)

    assert config.secrets.api_key == "environment-api-key"  # pragma: allowlist secret


@pytest.mark.parametrize("field", ["api_key", "token", "password", "username", "trade_key"])
def test_load_config_rejects_sensitive_fields_anywhere(tmp_path: Path, field: str) -> None:
    config_path = tmp_path / "unsafe.yaml"
    config_path.write_text(
        f"environment: demo\nstrategy:\n  {field}: should-not-be-here\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field):
        load_config(config_path)


def test_load_config_reports_missing_environment_variable_name_only(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("environment: demo\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="TRADER_API_KEY") as error:
        load_config(config_path)

    assert "secret" not in str(error.value).lower()


def test_load_config_accepts_one_minute_sma_testing_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\nstrategy:\n  sma_period_type: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    config = load_config(config_path)

    assert config.strategy.sma_period_type == 1


def test_load_config_accepts_hourly_sma_testing_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\nstrategy:\n  sma_period_type: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    config = load_config(config_path)

    assert config.strategy.sma_period_type == 2


def test_load_config_rejects_unsupported_sma_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\nstrategy:\n  sma_period_type: 4\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="sma_period_type"):
        load_config(config_path)


def test_load_config_rejects_unsupported_ema_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "environment: demo\nstrategy:\n  ema_period_type: 4\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TRADER_API_KEY", "api-key")  # pragma: allowlist secret

    with pytest.raises(ConfigError, match="ema_period_type"):
        load_config(config_path)
