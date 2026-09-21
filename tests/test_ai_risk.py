import httpx
import pytest

from trader_api_examples.ai_risk import (
    AccountContext,
    OpenAIRiskAgent,
    RiskAgentError,
)


def context() -> AccountContext:
    return AccountContext(
        environment="replay",
        contract="LLG",
        signal="OPEN_BUY",
        bar_time_ms=1_700_000_000_000,
        close=2_000.0,
        fast_average=2_001.0,
        slow_average=2_000.0,
        atr=10.0,
        spread_bps=2.65,
        equity=100_000.0,
        daily_pnl_pct=-0.2,
        drawdown_pct=1.5,
        open_positions=0,
        market_data_fresh=True,
    )


def test_openai_risk_agent_parses_bounded_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        200,
        json={
            "output_text": '{"action":"REDUCE","reason":"Volatility is elevated.","confidence":0.8}'
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        seen["url"] = url
        seen["kwargs"] = kwargs
        return response

    monkeypatch.setattr(httpx, "post", fake_post)

    agent = OpenAIRiskAgent(
        api_key="test-key",  # pragma: allowlist secret
        model="test-model",
        endpoint="https://api.openai.com/v1/responses",
        timeout_seconds=5.0,
    )
    decision = agent.decide(context())

    assert decision.action == "REDUCE"
    assert decision.confidence == 0.8
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert "Authorization" in seen["kwargs"]["headers"]  # type: ignore[index]


def test_openai_risk_agent_rejects_invalid_action(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        200,
        json={"output_text": '{"action":"BUY","reason":"trade","confidence":1}'},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: response)
    agent = OpenAIRiskAgent(
        api_key="test-key",  # pragma: allowlist secret
        model="test-model",
        endpoint="https://api.openai.com/v1/responses",
        timeout_seconds=5.0,
    )

    with pytest.raises(RiskAgentError, match="invalid action"):
        agent.decide(context())


def test_openai_risk_agent_requires_credentials() -> None:
    with pytest.raises(RiskAgentError, match="OPENAI_API_KEY"):
        OpenAIRiskAgent(
            api_key="",
            model="test-model",
            endpoint="https://api.openai.com/v1/responses",
            timeout_seconds=5.0,
        )
