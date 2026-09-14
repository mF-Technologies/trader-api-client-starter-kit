# SMA Week 3 Evaluation Protocol

Date: 2026-09-14
Branch: `codex/setup-pr`
Status: protocol locked before the formal holdout evaluation

This document records the Week 3 parameter-comparison protocol. It is research
evidence only and is not a claim that the strategy is ready for live trading.

## Checklist Progress

| Week 3 item | Current status | Evidence or remaining work |
|---|---|---|
| Mentor execution-readiness review | Reported complete | Mentor confirmation is not stored in this repository. |
| Approved minimum-size demo lifecycle | Pending | Current account has existing positions; the lifecycle checker requires an isolated flat account. |
| Reconcile orders, fills, positions, and account state | Pending | Earlier read-only checks succeeded, but recent account-balance and WebSocket HTTP 500 errors must be resolved before reconciliation. |
| Compare 10-20 transparent parameter combinations | Complete for development | All 15 candidates were replayed under the fixed expected-cost protocol. |
| Time-ordered out-of-sample comparison | Gate not reached | Earlier 70/30 and six-block H1 tests exist; no formal Week 3 candidate qualified for holdout promotion. |
| Apply the predeclared selection rule | Applied: no candidate selected | The rule was applied to development results only; it rejected the full set. |
| Document limitations, rejected variants, and remaining risks | Updated | Prior Week 2 logs contain the main limitations; this document records the formal comparison and failed selection gate. |
| Final PR and 15-20 minute walkthrough | Pending | Complete after the offline evaluation and demo evidence are reconciled. |

## Data and Fixed Assumptions

- Fixture: `work/xauusd_hourly_contiguous.json`.
- Instrument mapping: uploaded XAUUSD history; API contract is LLG.
- History: 62,851 contiguous H1 bars from 2016-01-18 02:00 UTC through 2026-09-03 17:00 UTC.
- Development window: indexes `[0, 43995)`.
- Holdout window: indexes `[43995, 62851)`; preceding bars are indicator warm-up only.
- Strategy: long-only SMA20/SMA50 with ATR14.
- Signal timing: completed-bar signal and next-bar execution.
- Amount: 10 API units for replay.
- Holding cap: 120 hours.
- Risk limits: 1% maximum trade loss, 2% maximum daily loss, and 10% maximum drawdown.
- Expected replay costs: commission `0.05` per API amount per side, total spread `2.65` bps, slippage `0.5` bps per side, financing `2` bps/day, and market impact `0.5` bps per side.
- Annualisation: 6,048 H1 observations per year.

The cost values are labelled assumptions. They are not broker-confirmed LLG
terms. The `$7` round-turn example is not used as a parameter here.

## Formal Candidate Set

The formal comparison contains 15 combinations: every previously tested exit
buffer and each of the three middle protective-stop multipliers.

- Exit buffer ATR multiples: `0`, `0.25`, `0.5`, `0.75`, `1`.
- Protective stop ATR multiples: `2`, `3`, `4`.
- Candidate count: `5 x 3 = 15`.

The candidates are:

| ID | Buffer ATR | Stop ATR |
|---:|---:|---:|
| C01 | 0 | 2 |
| C02 | 0.25 | 2 |
| C03 | 0.5 | 2 |
| C04 | 0.75 | 2 |
| C05 | 1 | 2 |
| C06 | 0 | 3 |
| C07 | 0.25 | 3 |
| C08 | 0.5 | 3 |
| C09 | 0.75 | 3 |
| C10 | 1 | 3 |
| C11 | 0 | 4 |
| C12 | 0.25 | 4 |
| C13 | 0.5 | 4 |
| C14 | 0.75 | 4 |
| C15 | 1 | 4 |

The earlier 25-cell sweep included stop values `1` and `5` as exploratory edge
variants. They are excluded from this 15-cell formal comparison to keep the
Week 3 candidate set within the requested 10-20 range. This exclusion is a
limitation and will be disclosed; stop `1` and stop `5` must not be added after
looking at holdout results.

## Predeclared Selection Rule

1. Run all 15 candidates on the development window using the fixed assumptions
   above. Do not inspect or use holdout metrics during selection.
2. A candidate is eligible only if the replay completes without a data-quality
   failure, has at least 30 closed trades, has a finite Sharpe ratio, and has a
   positive net strategy return.
3. Select the eligible candidate with the highest annualised strategy Sharpe.
   Break ties with higher net return, then lower maximum drawdown.
4. If no candidate is eligible, select no candidate and fail the selection gate;
   do not use holdout results to rescue a candidate.
5. Run the selected candidate, if one exists, exactly once on the holdout
   window. Report strategy return, buy-and-hold return, excess return, Sharpe,
   maximum drawdown, trade count, win rate, average holding time, risk-limit
   triggers, and blocked entries.

The holdout is an evaluation, not another optimization round. No buffer, stop,
cost, or risk limit may be changed after viewing its result.

## Development Comparison Result

All 15 replays completed without a data-quality failure. Net strategy return was
negative for every candidate, ranging from `-0.64%` to `-1.50%`. The table below
is the complete development comparison; Sharpe is annualised and drawdown is
shown as a positive loss percentage.

| ID | Buffer ATR | Stop ATR | Net return | Sharpe | Max drawdown | Trades |
|---:|---:|---:|---:|---:|---:|---:|
| C01 | 0 | 2 | -0.95% | -0.027 | 10.14% | 61 |
| C02 | 0.25 | 2 | -1.03% | -0.029 | 10.05% | 53 |
| C03 | 0.5 | 2 | -1.10% | -0.034 | 10.03% | 46 |
| C04 | 0.75 | 2 | -1.50% | -0.052 | 10.13% | 47 |
| C05 | 1 | 2 | -1.46% | -0.051 | 10.10% | 47 |
| C06 | 0 | 3 | -0.64% | -0.001 | 10.04% | 104 |
| C07 | 0.25 | 3 | -1.24% | -0.031 | 10.24% | 55 |
| C08 | 0.5 | 3 | -1.09% | -0.027 | 10.02% | 43 |
| C09 | 0.75 | 3 | -1.37% | -0.041 | 10.02% | 37 |
| C10 | 1 | 3 | -1.46% | -0.036 | 10.10% | 49 |
| C11 | 0 | 4 | -1.29% | -0.033 | 10.58% | 61 |
| C12 | 0.25 | 4 | -1.07% | -0.026 | 10.08% | 45 |
| C13 | 0.5 | 4 | -1.32% | -0.037 | 10.23% | 38 |
| C14 | 0.75 | 4 | -1.40% | -0.041 | 10.05% | 35 |
| C15 | 1 | 4 | -1.42% | -0.037 | 10.06% | 40 |

C06 was the best development result by the predeclared Sharpe ordering, but it
did not pass the positive-net-return eligibility gate. Therefore no candidate
was promoted to the formal Week 3 holdout run. The existing frozen H1 holdout
and six-block results remain valid separate evidence, but they must not be
described as an out-of-sample result for a selected candidate from this 15-cell
comparison.

## Non-Selection Diagnostic Holdout

To investigate the failed gate without changing the selection rule, C06 was run
once on the untouched holdout. This is diagnostic evidence only; C06 was not a
formal selected candidate.

| Metric | C06 strategy | Buy-and-hold |
|---|---:|---:|
| Net return | -3.03% | 110.86% |
| Sharpe | -0.165 | 1.212 |
| Maximum drawdown | 10.12% | 31.95% |
| Trades | 62 | n.a. |
| Win rate | 33.87% | n.a. |

C06 also triggered the maximum trade-loss limit 4 times, the daily-loss limit
once, and the drawdown limit once. The diagnostic holdout therefore does not
rescue the strategy: the least-bad development candidate also failed on unseen
data. A new strategy or cost/data experiment is required; changing the
selection rule after these results would be research leakage.

## Remaining Live-Evidence Risks

- The minimum-size demo lifecycle is not complete because the current account is
  not isolated and contains existing positions.
- Recent `/fapi/accountBalance` and PriceAgent WebSocket HTTP 500 responses mean
  the account state and live quote path are not currently reliable.
- Rotating the API key changes the local account fingerprint; the saved risk state
  must be reconciled before it is reset or reused.
- Financing/swap units, exit commission, and size-dependent market impact remain
  unconfirmed broker terms.
- A live client-side protective stop depends on fresh quotes and polling; a data
  outage can delay its reaction.
- The H1 frozen validation previously failed its robustness gate, especially
  under stressed costs. A selected candidate is not evidence of profitability.

## Latest execution-readiness attempt

- On 2026-09-14, the read-only `account-inspector` was run against the demo LLG
  account.
- The request was blocked before account data was returned because
  `/wp/api/tokens/auth` returned HTTP 500.
- No order, liquidation, position, journal, or account mutation occurred.
- Reconciliation and the minimum-size lifecycle remain pending until the
  authentication endpoint returns successfully and the account state can be
  inspected.
