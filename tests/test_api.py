from datetime import UTC, datetime

import httpx
import pytest

from trader_api_examples.api import ApiError, TraderApiClient


@pytest.mark.asyncio
async def test_client_exchanges_key_and_reads_account_state() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tokens/auth":
            assert request.headers["Authorization"] == "Bearer api-key"
            return httpx.Response(200, json={"access_token": "fx-token", "expires_in": 60})
        if request.url.path == "/accountBalance":
            assert request.headers["Authorization"] == "Bearer fx-token"
            return httpx.Response(200, json={"balance": 1234.5, "currency": "USD"})
        if request.url.path == "/api/getPositionToday":
            return httpx.Response(200, json={"positions": [{"ref": "42"}]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        balance = await client.get_account_balance()
        positions = await client.get_positions()

    assert balance == {"balance": 1234.5, "currency": "USD"}
    assert positions == [{"ref": "42"}]
    assert [request.url.path for request in requests].count("/api/tokens/auth") == 1


@pytest.mark.asyncio
async def test_client_fetches_completed_chart_bars_only() -> None:
    now = datetime(2026, 8, 26, 12, 0, 30, tzinfo=UTC)
    completed_start = int(datetime(2026, 8, 26, 11, 59, tzinfo=UTC).timestamp() * 1000)
    open_start = int(datetime(2026, 8, 26, 12, 0, tzinfo=UTC).timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chartCode":
            return httpx.Response(200, json={"EURUSD": "EURUSDUSD"})
        if "getChartBarsByEndTimeAndBarNumber" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"time": completed_start, "open": 1, "high": 2, "low": 0, "close": 1.5},
                    {"time": open_start, "open": 1.5, "high": 2, "low": 1, "close": 1.7},
                ],
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        bars = await client.get_completed_bars(contract="EURUSD", period_type=1, count=100, now=now)

    assert [bar.time_ms for bar in bars] == [completed_start]


@pytest.mark.asyncio
async def test_http_error_preserves_status_and_rejection_certainty() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"msg": "Not Available to trade this contract"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        with pytest.raises(ApiError) as caught:
            await client.add_market_deal(
                contract="LLS", amount=1000, buy=False, client_order_id=123
            )

    assert caught.value.status_code == 400
    assert caught.value.error_code == "Not Available to trade this contract"
    assert caught.value.is_definitive_rejection is True
