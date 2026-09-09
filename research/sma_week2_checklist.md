# SMA Week 2 Checklist Evidence

Date: 2026-09-09
Branch: `codex/setup`

This log records the checklist work executed against the current dirty working
tree. It is evidence for review; it is not a claim that the strategy is ready for
live trading.

## Fixed replay protocol

- Fixture: `work/xauusd_hourly_contiguous.json`
- Instrument mapping: uploaded XAUUSD history; account contract is LLG.
- History: 62,851 contiguous H1 bars, 2016-01-18 02:00 UTC through 2026-09-03 17:00 UTC.
- Development window: indexes `[0, 43995)`.
- Holdout window: indexes `[43995, 62851)`; prior bars are warm-up only.
- Strategy: long-only SMA20/SMA50, ATR14, zero exit buffer, ATR stop multiple 2.
- Execution: completed-bar signal, next-tradable-bar fill.
- Controls: one strategy-owned position, 120-hour maximum holding time, 1% maximum
  trade loss, 2% daily loss, and 10% maximum drawdown.
- Annualisation: 6,048 H1 observations per year.

Metrics were fixed before comparing cases: strategy net return, buy-and-hold net
return, excess return, annualised Sharpe, maximum drawdown, trade count, risk-limit
triggers, and blocked-entry count.

## Replay and transaction-cost evidence

The expected-cost holdout replay completed successfully:

| Metric | Result |
|---|---:|
| Strategy return | 98.98% |
| Buy-and-hold return | 110.86% |
| Strategy Sharpe | 2.01 |
| Strategy maximum drawdown | 10.04% |
| Trades | 175 |
| Maximum trade-loss triggers | 17 |
| Daily-loss triggers | 4 |
| Drawdown triggers | 1 |
| Blocked entries | 30 |

Expected-cost assumptions were commission `0.05` per API amount per side, total
spread `2.65` bps, slippage `0.5` bps per side, financing `2` bps/day, and market
impact `0.5` bps per side.

The same holdout and limits were used for cost sensitivity:

| Case | Commission | Spread | Slippage | Financing | Impact | Strategy return | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| Favourable | 0.05 | 2 bps | 0.11 bps | 0 bps/day | 0 bps | 112.77% | 2.33 |
| Expected | 0.05 | 2.65 bps | 0.5 bps | 2 bps/day | 0.5 bps | 98.98% | 2.01 |
| Stressed | 0.10 | 4 bps | 1 bps | 5 bps/day | 2 bps | -5.67% | -0.37 |

These are replay assumptions. Commission and spread are proxies grounded in the
demo checks; financing and market impact remain unconfirmed account terms. The `$7`
round-turn-per-lot figure used in documentation is illustrative only, not the frozen
commission value and not a broker-confirmed charge. Replay commission values are inputs
we provide from the broker schedule, statement, or a deliberately labelled sensitivity
scenario; they are not learned or chosen by the strategy.

## Limit and execution-safety evidence

The replay engine accepts and reports position, holding-time, trade-loss, daily-loss,
and drawdown limits. The expected case exercised all five controls. Runtime is now
separate from live holding time: the 600-second runtime is a flat signal-search budget,
while an open live SMA position uses the 120-hour holding limit and persisted entry/stop
state. Live SMA execution now also loads account equity, tightens the entry stop to the
configured trade-loss budget, persists daily and peak-equity state, closes an owned
position on a daily-loss or drawdown breach, and blocks further execution. Daily state
rolls at the next UTC date; drawdown remains latched across restarts. An unrelated open
account position hard-blocks a new live SMA submission, enforcing the one-position rule.

Duplicate and restart safeguards are covered by the passing execution tests:

- An unresolved execution journal blocks a second submission.
- A persisted open journal can be loaded by a restarted manager and reconciled.
- The live loop processes a completed bar only once using its timestamp.

The live daily-loss and drawdown paths are covered by offline tests with a fake account
balance and fake owned position. A controlled demo test with an actual broker position
and a real account-equity breach remains outstanding, as does a real broker-position
restart test.

## Controlled demo audit status

The authenticated REST account check succeeded in the demo environment. The broker
reported eight open positions, so the execution-cost round trip was not submitted; the
audit requires a verified flat account before it can add a test position. The LLG
contract settings reported a minimum amount of `10` API units, equal to `0.1` lot, for
the eventual minimum-size round trip.

The read-only execution-cost snapshot could not complete because the configured price
WebSocket returned HTTP 500 during quote collection. No order was submitted. Therefore
there is still no broker-confirmed exit commission, financing/swap charge, or market
impact measurement. The replay values remain labelled assumptions until a flat demo
account, working quote session, and controlled round trip or rollover observation are
available.

## Live-observe evidence

Command run:

```powershell
trader-api-examples sma-algo-demo --config config.local.yaml --mode live-observe --output json
```

The first attempt authenticated successfully but returned `INCONCLUSIVE` because the
latest completed M1 bar was approximately 120 seconds old. No order was submitted.
This was a valid data-health failure, not a strategy result.

A retry completed the full 600-second observation window and returned `NO_SIGNAL`:
no SMA crossover occurred before the runtime limit. No order was submitted. This
provides a successful live-observe safety run, but it is not evidence of profitability
because no trade signal occurred.

## Verification

- Full test suite: 91 passed.
- Ruff: passed.
- Mypy: passed.
- No live order mutation was enabled or submitted.

## Checklist status

- Replay historical data: completed for the fixed H1 development/holdout protocol.
- Define evaluation metrics: completed and recorded above before comparison.
- Add transaction-cost assumptions: implemented and exercised in three cases.
- Add limits: replay and live controls implemented and unit-tested; a controlled demo
  breach test remains outstanding before relying on them for live execution.
- Validate duplicate-signal and restart behaviour: automated journal and same-bar
  safeguards passed; live restart with an actually open broker position remains to be
  exercised.
- Run `live-observe` without orders: attempted safely; inconclusive because of stale
  data, with no order submitted.
- Update experiment log and PR evidence: this file, the H1 validation report, README,
  and automated tests provide the current evidence. No PR has been opened yet.
