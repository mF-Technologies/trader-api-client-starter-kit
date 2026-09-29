# Trader API Examples

Runnable Python reference tools for the mFT Trader API. The
[Developer Platform](https://mf-technologies.github.io/Developers-Platform/) is the source
of truth for authentication, API contracts, concepts, and first-trade guidance.

These examples are educational references, not investment advice or a production trading
system.

## What this repository demonstrates

- WebProxy API-key exchange and account reads
- Account-specific live quotes through `fxserverclientpython`
- Historical bars through Realtime Chart Server
- Orders, market deals, and liquidations through FxServer REST
- RSI algo execution with multiple independent instances

## Quick start

Requirements: Python 3.12, a demo account, and connection settings plus an API key from
API Key Management.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,prices]" --extra-index-url https://mf-technologies.github.io/Python-package-repositories/simple/
Copy-Item config.example.yaml config.local.yaml
Copy-Item .env.local.example .env.local
```

Copy the account connection settings into `config.local.yaml`. Put credentials in
`.env.local`:

```text
TRADER_API_KEY=<api-key-from-api-key-management>
TRADER_API_USERNAME=<username-from-api-key-management>
TRADER_API_TRADE_KEY=<trade-key-from-api-key-management>
TRADER_API_ENABLE_LIVE_TRADING=false
```

The REST client exchanges the API key through WebProxy `POST /api/tokens/auth` and uses the
short-lived access token for FxServer REST. The Python price client receives the API key as
`valid_generated_token` and handles its supported login; do not replace it with the access
token.

## Verify the connection

```powershell
trader-api-examples account-inspector --config config.local.yaml
trader-api-examples market-data-monitor --config config.local.yaml
trader-api-examples market-data-monitor --config config.local.yaml --bars-only
```

For a no-network strategy check:

```powershell
trader-api-examples rsi-algo-demo --config config.local.yaml --mode replay
```

## Run an algo

`algo-runner` shares one live price session, reads completed Chart Server bars, and sends
trading mutations through FxServer REST. Configure one or more entries under `instances`;
each entry can select `rsi`, `ema_cross`, `sma_cross`, or `macd`.

Observe signals without trading:

```powershell
trader-api-examples algo-runner --config config.local.yaml
```

Replay is the default for `rsi-algo-demo`. Live execution requires both gates:

```powershell
$env:TRADER_API_ENABLE_LIVE_TRADING = "true"
trader-api-examples algo-runner --config config.local.yaml --mode live-execute --execute
```

Use a demo environment first. The runner writes per-instance logs and recovery journals
under `runtime/`; keep those files private because they can contain account and trade state.

## Phase 1 request flow

1. Create an API key with `read` and, when needed, `trade` permission in API Key Management.
2. Use the supplied connection settings and API key in the local files.
3. Use `fxserverclientpython` for live bid/ask prices and price tags.
4. Resolve contract to chart code with FxServer, then request completed bars from ChartServer.
5. Use FxServer REST for account reads, orders, deals, and liquidations.
6. Use `GET /updateEventStream` for asynchronous notifications and confirm state with REST.

Read the public guides for the complete request and response schemas:

- [Make your first trade](https://mf-technologies.github.io/Developers-Platform/docs/getting-started/first-trade)
- [Get live prices](https://mf-technologies.github.io/Developers-Platform/docs/getting-started/get-prices)
- [FxServer Trader API](https://mf-technologies.github.io/Developers-Platform/docs/fx-server/openapi-trader)
- [Realtime Chart Server](https://mf-technologies.github.io/Developers-Platform/docs/realtime-chart-server/overview)

See [SECURITY.md](SECURITY.md) before sharing logs or diagnostics.

## License

This repository is licensed under the Apache License 2.0. See [LICENSE](LICENSE).
