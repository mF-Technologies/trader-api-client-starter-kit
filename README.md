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
Copy-Item .env.local.example .env.local
```

Set the demo endpoints and instrument settings in `config.local.yaml`, then add credentials
to `.env.local`. The application automatically reads `.env.local` from the same directory
as the selected configuration file. Existing process environment variables take precedence.
Both local files are ignored by Git.

## Commands

```powershell
trader-api-examples account-inspector --config config.local.yaml
trader-api-examples contract-calculator --config config.local.yaml --lots 0.01
trader-api-examples execution-cost-audit --config config.local.yaml --samples 60 --interval-seconds 1
trader-api-examples market-data-monitor --config config.local.yaml
trader-api-examples order-lifecycle-checker --config config.local.yaml
trader-api-examples rsi-algo-demo --config config.local.yaml --mode replay
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --buffer 0.5
trader-api-examples ema-algo-demo --config config.local.yaml --mode replay --buffer 0.5
trader-api-examples ema-algo-demo --config config.local.yaml --mode live-observe
trader-api-examples sma-data-export --config config.local.yaml --output-file work/llg-1m.json --count 5000
```

Every command supports `--output human` (default) or `--output json`. Logs go to stderr;
the result goes to stdout.

`market-data-monitor` reads both the current quote and completed historical bars. Use
`--bars-only` when validating Chart Server without opening a price session.

## Execution Cost Audit

`execution-cost-audit` defaults to a read-only snapshot. It records the current LLG
contract settings, account balance, order state, and bid/ask samples. The quote summary
reports spread in price units and basis points. Use `--samples` and
`--interval-seconds` to collect a short spread sample without submitting an order.

Round-trip mode is restricted to the demo environment and requires both
`TRADER_API_ENABLE_LIVE_TRADING=true` and `--execute`. Use the account minimum amount
for the first test, not the normal strategy amount:

```powershell
trader-api-examples execution-cost-audit --config config.local.yaml --mode round-trip --amount 10 --hold-seconds 2 --execute --output json
```

The round-trip records quotes before entry and exit, balances, order state, position
details, and cleanup reconciliation. It does not infer financing/swap or market impact;
those require a rollover observation and size-specific execution evidence. Never use
round-trip mode on a live account.

If an account has existing demo positions, `--mode round-trip` refuses to submit a new
order. After verifying that all existing positions belong to the intended LLG audit,
they can be liquidated and verified flat with:

```powershell
trader-api-examples execution-cost-audit --config config.local.yaml --mode close-existing --execute --output json
```

This mode only operates in the demo environment and refuses to touch positions whose
contract does not match the configured contract.

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

## SMA20/SMA50 Algo

The SMA example is a long-only strategy whose default research contract is LLG. In this
account, `LLG` is the API contract code for XAUUSD/London Gold; historical exports may
therefore use `XAUUSD` in their filenames. Set
`strategy.sma_period_type` to `1` for one-minute bars, `2` for hourly bars, or `3` for daily bars. A bullish signal requires the completed-bar transition
`SMA20 <= SMA50` to `SMA20 > SMA50`. The signal is generated after that close and the
replay engine enters at the next bar open.

For real-time testing, set `strategy.sma_period_type` to `1` for one-minute bars or `2` for hourly bars. The live command polls
completed bars, evaluates a new signal when the latest bar closes, and
submits a market order immediately afterward, which is the next tradable bar. At least
51 completed bars are needed before SMA20, SMA50, and ATR14 can produce a
signal. `live-observe` reports the first signal without submitting an order.

Live data health fails closed when the latest completed bar is more than two expected
bar intervals old or when an unexpected internal gap is found. The default LLG session
settings allow the observed 23:00-01:00 UTC daily break and weekend closure. Verify
those hours against the account schedule and add known full-day holidays to
`trading.market_data_closed_dates_utc` in the local configuration. An internal gap is
reported as `INCONCLUSIVE`; the tool does not invent candles or submit a new order.

While long, the exit level is `SMA50 - (sma_exit_buffer_atr * ATR14)`. The default buffer
is zero, which is the unbuffered bearish crossover. A positive buffer must be crossed from
above, so it filters out small moves around the two averages. `atr_stop_multiple` sets the
protective stop from the next-bar entry price using the ATR measured on the signal bar:
`entry price - (ATR14 * atr_stop_multiple)`.

Replay reports strategy net return, a Sharpe ratio annualized for the selected bar interval, maximum drawdown, and
the same metrics for a buy-and-hold benchmark. `commission_rate` is the decimal commission
rate applied to entry and exit notional, and `slippage_bps` is the per-side slippage in
basis points. The replay also accepts explicit sensitivity assumptions: `commission_per_unit`
is fixed cash commission per API amount per side. `commission_round_turn_per_lot` is the
total entry-plus-exit commission for one lot; `trading.amount_per_lot` converts the API
amount into lots. For example, with `--commission-round-turn-per-lot 7` and
`--amount-per-lot 100`, an API amount of 100 incurs $7 for the complete round trip.
The `$7` value is illustrative only; it is not the broker's confirmed LLG commission
and must not be treated as a selected strategy parameter.
`spread_bps` is the full quoted spread,
`financing_bps_per_day` is a continuous long-carry proxy, and `market_impact_bps` is an
additional per-side fill penalty. Historical OHLC is treated as a midpoint: half the spread
is applied to each side, while slippage and impact are applied to the actual entry and exit
fills. These replay inputs are assumptions, not verified LLG account terms; financing and
market impact remain unconfirmed until account statements or size-controlled fills establish
them. Each trade reports `gross_pnl`, `commission`, `financing`, and `net_pnl`, where
`net_pnl = gross_pnl - commission - financing`. With financing set to zero, this is exactly
gross PnL minus commission.

Run the buffer sweep by repeating replay with values such as `0`, `0.25`, `0.5`, and `0.75`.
For cost sensitivity, keep the research baseline fixed and change only the cost flags. For
example, the following illustrative cases use the same H1 fixture, buffer `0`, ATR stop
multiple `2`, 120-hour holding cap, 1% trade-loss cap, 2% daily-loss cap, and 10% drawdown cap:

```powershell
# Favourable: observed entry commission proxy, tight spread/slippage, no unconfirmed costs
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --evaluation-start-index 44170 --amount 10 --annualization-factor 6048 --buffer 0 --stop-multiple 2 --max-holding-hours 120 --max-trade-loss-pct 1 --max-daily-loss-pct 2 --max-drawdown-pct 10 --commission-per-unit 0.05 --spread-bps 2 --slippage-bps 0.11 --financing-bps-per-day 0 --market-impact-bps 0 --output json

# Expected: quoted spread proxy plus modest carry/execution uncertainty
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --evaluation-start-index 44170 --amount 10 --annualization-factor 6048 --buffer 0 --stop-multiple 2 --max-holding-hours 120 --max-trade-loss-pct 1 --max-daily-loss-pct 2 --max-drawdown-pct 10 --commission-per-unit 0.05 --spread-bps 2.65 --slippage-bps 0.5 --financing-bps-per-day 2 --market-impact-bps 0.5 --output json

# Stressed: wider spread, worse fills, and explicit carry/impact stress proxies
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --evaluation-start-index 44170 --amount 10 --annualization-factor 6048 --buffer 0 --stop-multiple 2 --max-holding-hours 120 --max-trade-loss-pct 1 --max-daily-loss-pct 2 --max-drawdown-pct 10 --commission-per-unit 0.10 --spread-bps 4 --slippage-bps 1 --financing-bps-per-day 5 --market-impact-bps 2 --output json
```

These three cases are sensitivity bounds, not evidence that the broker charges those exact
values. The fixed commission and spread proxies are grounded in the observed demo checks;
financing and market impact are deliberately scenario assumptions because the earlier audit
did not identify their units or a reliable size-dependent effect.

Use `--max-holding-hours` to test a time-based exit. Replay closes at the first next-bar
open where the elapsed holding time reaches the limit and records `MAX_HOLDING_TIME` as the
exit reason. For example:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --max-holding-hours 72
```

Replay-only risk controls accept percentage points, so `--max-trade-loss-pct 1` means 1%.
The per-trade limit tightens the ATR stop when necessary. The daily-loss limit and drawdown
limit force an exit on the next bar and block new entries for the rest of the UTC day or
the remainder of the replay, respectively. Use them in staged comparisons:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --max-trade-loss-pct 1 --max-daily-loss-pct 2 --max-drawdown-pct 10
```

To backtest real completed LLG candles, set `strategy.sma_period_type` to `1` for one-minute,
`2` for hourly, or `3` for daily bars. Export them first and pass the resulting fixture to
replay. For example, with hourly mode:

```powershell
trader-api-examples sma-data-export --config config.local.yaml --output-file work/llg-hourly.json --count 1000
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture work/llg-hourly.json --buffer 0.5
```

Replay supports a chronological development/holdout split without creating separate
fixture files. Indexes are zero-based and `--evaluation-end-index` is exclusive. For a
1,000-bar fixture, use the first 700 bars to select parameters:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture work/llg-hourly.json --evaluation-end-index 700 --buffer 0 --stop-multiple 4
```

After locking the selected parameters, evaluate the unseen final 300 bars:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture work/llg-hourly.json --evaluation-start-index 700 --buffer 0 --stop-multiple 4
```

The holdout run uses bars before index 700 only for SMA/ATR warm-up, starts with no
position, and scores strategy and buy-and-hold metrics from index 700 onward. At least
`sma_slow_period + 1` prior bars are required for a nonzero evaluation start index.

The SMA replay also accepts tab-delimited market exports with `<DATE>`, `<TIME>`,
`<OPEN>`, `<HIGH>`, `<LOW>`, and `<CLOSE>` columns, such as:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --fixture .\XAUUSD_H1_200701010300_202609031700.csv --evaluation-start-index 44170 --buffer 0 --stop-multiple 4
```

CSV timestamps are treated as UTC for ordering. The strategy uses only OHLC values; tick
volume, real volume, and spread columns are currently ignored. In particular, a CSV spread
column cannot by itself establish the account's commission, financing, or execution costs.
If a source export contains a multi-month or multi-year gap, split or normalize it into a
contiguous segment before replay so SMA/ATR indicators do not bridge the gap.

The exporter uses `strategy.sma_period_type`, removes the still-forming candle, and writes the
`time`, `open`, `high`, `low`, and `close` fields expected by `--fixture`. The requested count is
subject to the chart service history limit; repeat with a smaller count if the service rejects
the request.

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --buffer 0
trader-api-examples sma-algo-demo --config config.local.yaml --mode replay --buffer 0.25
# Set strategy.sma_period_type: 2 for hourly or 1 for one-minute live testing.
trader-api-examples sma-algo-demo --config config.local.yaml --mode live-observe
$env:TRADER_API_ENABLE_LIVE_TRADING = "true"
trader-api-examples sma-algo-demo --config config.local.yaml --mode live-execute --execute
```

Live execution uses the existing two mutation gates and journal reconciliation. The
protective stop is monitored client-side from the current bid because the example API
surface does not expose a native stop-order endpoint; it is therefore subject to polling
latency and gaps. A data-health failure blocks new actions, but it cannot make a
client-side stop react while the process has no fresh data. Live execution remains disabled unless both
`TRADER_API_ENABLE_LIVE_TRADING=true` and `--execute` are supplied.

For the live SMA loop, `trading.max_runtime_seconds` is a flat signal-search budget. When
the strategy is flat and no signal appears, the process stops at that limit. An open
position is governed separately by `trading.max_holding_hours` (120 hours by default) and
continues to be managed until the bearish crossover, protective stop, or holding-time exit.
The live loop also enforces the one-open-position rule by blocking when it finds an
unrelated account position.
The execution journal persists the entry timestamp, entry snapshot, and protective stop so
a restart can resume the same owned position. `max_trade_loss_pct` tightens the ATR stop
from the current account equity. `max_daily_loss_pct` closes an owned position and blocks
new entries until the next UTC date; `max_drawdown_pct` does the same and remains latched
across restarts. These account-level values are stored in `runtime/risk-<contract>.json`
and require the balance response to expose a positive `equity`, `balance`, or `available`
value. A process crash still creates a monitoring gap; use `recover` when ownership or
journal state is uncertain.

## EMA20/EMA50 Algo

`ema-algo-demo` is a separate moving-average experiment that uses EMA20 and EMA50 with
the same long-only crossover, ATR14 buffer, ATR stop, next-bar replay execution, costs,
and evaluation metrics as the SMA example. Its `strategy.ema_period_type` setting is
optional; when omitted, it follows `strategy.sma_period_type` so the two experiments use
the same candle interval. The EMA implementation and output use `EMA` names and do not
modify the SMA command.

EMA supports `replay` for historical testing and `live-observe` for read-only real-time
signal monitoring. EMA live order submission is intentionally disabled for this separate
experiment, so running it beside `sma-algo-demo` cannot create a second strategy-owned
position or reuse the SMA execution journal.

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

The current SMA research target is LLG, the account's API code for XAUUSD/London Gold.
Treat LLG API responses and XAUUSD historical files as the same instrument only when this
account mapping is confirmed; do not silently combine data from a different gold symbol.

See [SUPPORT.md](SUPPORT.md) for issue routing and [SECURITY.md](SECURITY.md) before sharing
diagnostics.
