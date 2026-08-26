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
