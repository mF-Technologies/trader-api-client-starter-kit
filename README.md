# Trader API Examples

Runnable Python reference tools for the mFT Trader API. This repository shows how to
connect API capabilities into observable workflows; the
[Developer Platform](https://mf-technologies.github.io/Developers-Platform/) remains the
source of truth for concepts, authentication, API reference, and first-trade guidance.

This repository is private and pre-release. The examples are educational references, not
investment advice, a profitability claim, or a production trading system.

## Requirements

- Python 3.12
- Trader API settings and API key from API Key Management
- Windows 11 for the currently verified local workflow

Linux and macOS are best effort until they are verified against the mFT package and live
services.

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,prices]" --extra-index-url https://mf-technologies.github.io/Python-package-repositories/simple/
Copy-Item config.example.yaml config.local.yaml
Copy-Item .env.example .env
```

Load the values from `.env` into your shell or secret manager. The application reads
credentials only from environment variables; it does not parse `.env` automatically.

## Commands

```powershell
trader-api-examples account-inspector --config config.local.yaml
trader-api-examples contract-calculator --config config.local.yaml --lots 0.01
trader-api-examples market-data-monitor --config config.local.yaml
trader-api-examples order-lifecycle-checker --config config.local.yaml
trader-api-examples rsi-algo-demo --config config.local.yaml --mode replay
```

Every command supports `--output human` (default) or `--output json`. Logs go to stderr;
the result goes to stdout.

`market-data-monitor` reads both the current quote and completed historical bars. Use
`--bars-only` when validating Chart Server without opening a price session.

## RSI Algo Demo

The demo uses TA-Lib RSI(14) over completed Realtime Chart Server bars:

- Open long when RSI crosses upward out of oversold (`<30` to `>=30`).
- Open short when RSI crosses downward out of overbought (`>70` to `<=70`).
- Close long or short when RSI crosses the neutral level (`50`) in the exit direction.
- Hold at most one owned position and complete at most one round trip per run.
- Do not average, martingale, repeat while inside a zone, or relax thresholds to create a signal.

Modes:

- `replay`: synthetic fixture, no network, no trading; this is the default.
- `live-observe`: live completed bars and proposed actions, no trading.
- `live-execute`: live bars plus real Trader API mutations.

`NO_SIGNAL` is a normal result. Missing, malformed, stale, or interrupted market data is
`INCONCLUSIVE` instead.

## Live Execution

Live execution is demo-first but technically compatible with eligible real accounts. Only
the demo workflow is verified for v1. Real-account use involves real financial risk and is
not an officially verified v1 workflow.

Both gates are required before any mutation:

```powershell
$env:TRADER_API_ENABLE_LIVE_TRADING = "true"
trader-api-examples rsi-algo-demo --config config.local.yaml --mode live-execute --execute
```

Before submitting, the tool displays a redacted account fingerprint, environment,
contract, side, and amount. It persists a minimal ignored recovery journal before sending
`addDeal`. The tool only closes positions whose ownership it can establish. Unrelated
positions produce a warning; unresolved or ambiguous ownership blocks new submissions.

The current API prevents duplicate requests with `clientOrderId`, but the current query
contracts do not expose a reliable `clientOrderId` correlation. When a submission outcome
is ambiguous, the tool fails closed and requires inspection in Trader Terminal.

## Scope

The v1 tools cover WebProxy token exchange/account state, FxServer Trader REST execution,
Realtime Chart Server completed bars, contract amount calculation, and an RSI reference
flow. They do not cover Terminal UI automation, CRM, payments, PAMM, MT5, deployment,
multi-instance bot orchestration, a strategy marketplace, or production operations.

See [SUPPORT.md](SUPPORT.md) for issue routing and [SECURITY.md](SECURITY.md) before sharing
diagnostics.
