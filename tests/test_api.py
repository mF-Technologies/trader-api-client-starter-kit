import asyncio
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
async def test_client_retries_transient_token_exchange_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_exchange_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        if request.url.path == "/api/tokens/auth":
            token_exchange_count += 1
            if token_exchange_count == 1:
                return httpx.Response(502, json={"msg": "Bad Gateway"})
            return httpx.Response(200, json={"access_token": "fx-token", "expires_in": 60})
        if request.url.path == "/accountBalance":
            assert request.headers["Authorization"] == "Bearer fx-token"
            return httpx.Response(200, json={"balance": 1234.5})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr("trader_api_examples.api.TOKEN_EXCHANGE_RETRY_DELAY_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        balance = await client.get_account_balance()

    assert balance == {"balance": 1234.5}
    assert token_exchange_count == 2


@pytest.mark.asyncio
async def test_client_watches_position_update_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr("trader_api_examples.api.POSITION_STREAM_RETRY_DELAY_SECONDS", 0.001)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/updateEventStream"
        assert request.headers["Authorization"] == "Bearer fx-token"
        if len(requests) > 1:
            assert request.headers["Last-Event-ID"] == "42"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(b'id: 42\nevent: PositionUpdate\ndata: {"deleted":[5178]}\n\n'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"
        queue = asyncio.Queue()
        stop_event = asyncio.Event()
        task = asyncio.create_task(client.watch_position_updates(queue, stop_event))

        notification = await asyncio.wait_for(queue.get(), timeout=0.2)
        for _ in range(100):
            if len(requests) > 1:
                break
            await asyncio.sleep(0.001)
        stop_event.set()
        await asyncio.wait_for(task, timeout=0.2)

    assert notification.event_id == "42"
    assert notification.affected_refs == frozenset({"5178"})
    assert len(requests) > 1


@pytest.mark.asyncio
async def test_position_update_stream_backs_off_after_repeated_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0
    stop_event = asyncio.Event()
    monkeypatch.setattr("trader_api_examples.api.POSITION_STREAM_RETRY_DELAY_SECONDS", 0.001)
    monkeypatch.setattr("trader_api_examples.api.POSITION_STREAM_MAX_RETRY_DELAY_SECONDS", 0.002)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        assert request.url.path == "/updateEventStream"
        requests += 1
        if requests >= 3:
            stop_event.set()
        return httpx.Response(401, json={"msg": "Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )

        async def auth_headers() -> dict[str, str]:
            return {"Authorization": "Bearer expired-token"}

        client._auth_headers = auth_headers  # type: ignore[method-assign]
        await asyncio.wait_for(client.watch_position_updates(asyncio.Queue(), stop_event), 0.2)

    assert requests == 3


@pytest.mark.asyncio
async def test_client_retries_token_exchange_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_exchange_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        if request.url.path != "/api/tokens/auth":
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        token_exchange_count += 1
        if token_exchange_count == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"access_token": "fx-token", "expires_in": 60})

    monkeypatch.setattr("trader_api_examples.api.TOKEN_EXCHANGE_RETRY_DELAY_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )

        assert await client.access_token() == "fx-token"

    assert token_exchange_count == 2


@pytest.mark.asyncio
async def test_client_does_not_retry_definitive_token_exchange_failure() -> None:
    token_exchange_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        if request.url.path != "/api/tokens/auth":
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        token_exchange_count += 1
        return httpx.Response(401, json={"msg": "Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        with pytest.raises(ApiError, match="HTTP 401"):
            await client.get_account_balance()

    assert token_exchange_count == 1


@pytest.mark.asyncio
async def test_client_refreshes_access_token_after_expiry() -> None:
    token_exchange_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        if request.url.path != "/api/tokens/auth":
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        token_exchange_count += 1
        return httpx.Response(
            200,
            json={"access_token": f"fx-token-{token_exchange_count}", "expires_in": 60},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        assert await client.access_token() == "fx-token-1"
        client._access_token_expires_at = 0
        assert await client.access_token() == "fx-token-2"

    assert token_exchange_count == 2


@pytest.mark.asyncio
async def test_client_retries_authenticated_request_after_unauthorized() -> None:
    token_exchange_count = 0
    account_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchange_count
        if request.url.path == "/api/tokens/auth":
            token_exchange_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"fx-token-{token_exchange_count}", "expires_in": 60},
            )
        if request.url.path == "/accountBalance":
            authorization = request.headers["Authorization"]
            account_requests.append(authorization)
            if authorization == "Bearer fx-token-1":
                return httpx.Response(401, json={"msg": "Unauthorized"})
            return httpx.Response(200, json={"balance": 1234.5})
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

    assert balance == {"balance": 1234.5}
    assert account_requests == ["Bearer fx-token-1", "Bearer fx-token-2"]
    assert token_exchange_count == 2


@pytest.mark.asyncio
async def test_add_market_deal_defers_netting_response_to_reconciliation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/addDeal":
            return httpx.Response(200, json={"netting": True, "liquidateRef": "5246"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        deal_ref = await client.add_market_deal(
            contract="USDCAD", amount=100000, buy=True, client_order_id=123
        )

    assert deal_ref is None


@pytest.mark.asyncio
async def test_liquidate_market_deal_accepts_netting_deal_reference() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/liquidate":
            return httpx.Response(200, json={"netting": True, "dealRef": "5250"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        cleanup_ref = await client.liquidate_market_deal(
            order_ref="5247", amount=100000, client_order_id=124
        )

    assert cleanup_ref == "5250"


@pytest.mark.asyncio
async def test_liquidate_market_deal_allows_missing_reference_for_reconciliation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/liquidate":
            return httpx.Response(200, json={"netting": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        cleanup_ref = await client.liquidate_market_deal(
            order_ref="5247", amount=100000, client_order_id=124
        )

    assert cleanup_ref is None


@pytest.mark.asyncio
async def test_reconcile_market_deal_uses_unique_matching_position() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/getOrderToday":
            return httpx.Response(
                200,
                json={"workingOrders": [], "executedOrders": [], "cancelledOrders": []},
            )
        if request.url.path == "/api/getPositionToday":
            return httpx.Response(
                200,
                json={
                    "positions": [
                        {
                            "ref": 5247,
                            "contract": "USDCAD",
                            "buySell": True,
                            "amount": 100000,
                        }
                    ]
                },
            )
        if request.url.path == "/positionDetail":
            assert request.url.params["ref"] == "5247"
            return httpx.Response(
                200,
                json={"orderNo": 5247, "contractCode": "USDCAD", "amount": 100000},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        deal_ref = await client.reconcile_market_deal(
            contract="USDCAD",
            amount=100000,
            buy=True,
            client_order_id=123,
            excluded_refs=(),
        )

    assert deal_ref == "5247"


@pytest.mark.asyncio
async def test_reconcile_market_deal_rejects_ambiguous_positions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/getOrderToday":
            return httpx.Response(200, json={})
        if request.url.path == "/api/getPositionToday":
            return httpx.Response(
                200,
                json={
                    "positions": [
                        {"ref": 5247, "contract": "USDCAD", "buySell": True, "amount": 100000},
                        {"ref": 5248, "contract": "USDCAD", "buySell": True, "amount": 100000},
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        client._access_token = "fx-token"

        deal_ref = await client.reconcile_market_deal(
            contract="USDCAD",
            amount=100000,
            buy=True,
            client_order_id=123,
            excluded_refs=(),
        )

    assert deal_ref is None


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
async def test_completed_bars_cache_chart_code_mapping() -> None:
    chart_code_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chart_code_requests
        if request.url.path == "/chartCode":
            chart_code_requests += 1
            return httpx.Response(200, json={"EURUSD": "EURUSD"})
        return httpx.Response(
            200,
            json=[{"time": 0, "open": 1, "high": 1, "low": 1, "close": 1}],
        )

    now = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://webproxy.example",
            fxserver_url="https://fxserver.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        await client.get_completed_bars(contract="EURUSD", period_type=1, count=100, now=now)
        await client.get_completed_bars(contract="EURUSD", period_type=1, count=100, now=now)

    assert chart_code_requests == 1


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
