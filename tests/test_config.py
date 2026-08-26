from pathlib import Path

import pytest

from trader_api_examples.config import ConfigError, load_config


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
  contract: EURUSD
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
    assert config.trading.contract == "EURUSD"
    assert config.trading.amount == 1000
    assert config.secrets.api_key == "secret-api-key"  # pragma: allowlist secret


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
