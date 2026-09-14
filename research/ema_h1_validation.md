# EMA H1 Validation

Date: 2026-09-14

This is a separate EMA comparison using the same data, rules, costs, limits, and
evaluation protocol used for the SMA validation. It is research evidence only and
does not establish that the strategy is ready for live trading.

## Data and fixed conditions

- Fixture: `work/xauusd_hourly_contiguous.json`.
- Instrument mapping: uploaded XAUUSD history; the API contract is LLG.
- History: 62,851 contiguous H1 bars from 2016-01-18 02:00 UTC through 2026-09-03 17:00 UTC.
- Development window: indexes `[0, 43995)`.
- Holdout window: indexes `[43995, 62851)`; earlier bars are indicator warm-up only.
- Strategy: long-only EMA20/EMA50 with ATR14.
- Signal timing: completed-bar signal and next-bar execution.
- Frozen baseline: exit buffer `0`, protective stop `2 ATR`, amount `10` API units.
- Limits: 120-hour maximum holding time, 1% maximum trade loss, 2% daily loss,
  and 10% maximum drawdown.
- Annualisation: 6,048 H1 observations per year.

The expected-cost case used commission `0.05` per API amount per side, total spread
`2.65` bps, slippage `0.5` bps per side, financing `2` bps/day, and market impact
`0.5` bps per side. The stressed case used `0.10`, `4`, `1`, `5`, and `2` respectively.
These are the same labelled assumptions used by the SMA tests, not broker-confirmed terms.

## Parameter tests

An exploratory 25-cell grid tested buffers `0`, `0.25`, `0.5`, `0.75`, `1` against
protective stops `1`, `2`, `3`, `4`, `5` on development data. The formal comparison
used the predeclared 15-cell subset with stops `2`, `3`, and `4`.

The selection rule was unchanged: a candidate needed at least 30 trades, finite Sharpe,
and positive net development return. Among eligible candidates, the highest Sharpe would
be selected, with return and drawdown as tie-breakers.

| Buffer ATR | Stop ATR | Net return | Sharpe | Max drawdown | Trades |
|---:|---:|---:|---:|---:|---:|
| 0 | 2 | -4.25% | -0.189 | 10.13% | 44 |
| 0.25 | 2 | -4.17% | -0.190 | 10.00% | 35 |
| 0.5 | 2 | -4.21% | -0.190 | 10.04% | 34 |
| 0.75 | 2 | -3.88% | -0.175 | 10.20% | 33 |
| 1 | 2 | -3.88% | -0.175 | 10.20% | 33 |
| 0 | 3 | -4.20% | -0.206 | 10.01% | 28 |
| 0.25 | 3 | -4.28% | -0.209 | 10.09% | 27 |
| 0.5 | 3 | -4.36% | -0.209 | 10.16% | 25 |
| 0.75 | 3 | -3.36% | -0.159 | 10.16% | 24 |
| 1 | 3 | -3.36% | -0.159 | 10.16% | 24 |
| 0 | 4 | -4.47% | -0.220 | 10.26% | 28 |
| 0.25 | 4 | -4.42% | -0.215 | 10.33% | 27 |
| 0.5 | 4 | -4.29% | -0.201 | 10.09% | 25 |
| 0.75 | 4 | -3.43% | -0.141 | 10.23% | 28 |
| 1 | 4 | -3.35% | -0.137 | 10.16% | 28 |

All 15 formal candidates had negative development returns. No candidate qualified, so
none was promoted using holdout results. The least-bad diagnostic candidate by the
Sharpe ordering was buffer `1`, stop `4`, but it returned `-3.35%` and had only 28 trades.

## Frozen holdout

The frozen baseline was evaluated once on the untouched holdout. This is not a selected
candidate; it is the direct EMA counterpart to the frozen SMA baseline.

| Metric | EMA strategy | Buy-and-hold |
|---|---:|---:|
| Net return | 62.80% | 110.86% |
| Excess return | -48.07 percentage points | n.a. |
| Sharpe | 1.468 | 1.212 |
| Maximum drawdown | 10.08% | 31.95% |
| Trades | 145 | n.a. |
| Win rate | 30.34% | n.a. |
| Average holding time | 50.85 hours | n.a. |
| Maximum holding time | 166 hours | n.a. |

The drawdown is constrained by the configured 10% drawdown limit. It should not be
interpreted as the natural drawdown of an unconstrained EMA strategy.

## Holdout cost sensitivity

| Case | Commission | Spread | Slippage | Financing | Impact | EMA return | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| Favourable | 0.05 | 2 bps | 0.11 bps | 0 bps/day | 0 bps | 78.29% | 1.783 |
| Expected | 0.05 | 2.65 bps | 0.5 bps | 2 bps/day | 0.5 bps | 62.80% | 1.468 |
| Stressed | 0.10 | 4 bps | 1 bps | 5 bps/day | 2 bps | -9.94% | -0.879 |

The EMA result becomes negative under the stressed cost case, so the same cost-sensitivity
failure seen in the SMA work remains present.

## Six chronological blocks

Each block started flat and used earlier bars only for indicator warm-up. The frozen
baseline was not retuned between blocks.

| Block | Expected return | Stressed return | Expected Sharpe | Stressed Sharpe |
|---:|---:|---:|---:|---:|
| 1: 2016-01 to 2017-10 | -4.25% | -4.41% | -0.387 | -0.441 |
| 2: 2017-10 to 2019-08 | -9.48% | -9.99% | -1.519 | -1.892 |
| 3: 2019-08 to 2021-05 | 15.84% | -8.84% | 0.917 | -1.214 |
| 4: 2021-05 to 2023-02 | -8.23% | -8.17% | -1.218 | -1.804 |
| 5: 2023-02 to 2024-11 | -4.49% | -4.84% | -0.602 | -0.645 |
| 6: 2024-11 to 2026-09 | 43.48% | 22.79% | 1.927 | 1.117 |

The expected-cost strategy was profitable in 2 of 6 blocks. The stressed-cost strategy
was profitable in 1 of 6 blocks. This fails the same consistency/robustness gate as SMA.

## Holding-time and risk-limit tests

Holding-time tests used the holdout, expected costs, and the frozen buffer/stop values.

| Maximum holding time | Return | Sharpe | Max drawdown | Trades |
|---:|---:|---:|---:|---:|
| 48 hours | 7.88% | 0.344 | 10.33% | 134 |
| 72 hours | 28.20% | 0.813 | 10.12% | 149 |
| 96 hours | 50.67% | 1.290 | 10.07% | 144 |
| 120 hours | 62.80% | 1.468 | 10.08% | 145 |

| Risk controls | Return | Max drawdown | Trades | Blocked entries |
|---|---:|---:|---:|---:|
| No account limits | 56.28% | 26.62% | 169 | 0 |
| Trade-loss limit only | 56.73% | 19.15% | 169 | 0 |
| Trade plus daily-loss limits | 59.80% | 18.79% | 169 | 0 |
| All frozen limits | 62.80% | 10.08% | 145 | 24 |

The limits materially reduce observed drawdown and change the trade path. They are risk
controls, not evidence that the underlying signal is more profitable.

## Runtime data-loss fault injection

Ten deterministic outage scenarios were run for each duration, removing consecutive H1
bars ending at a baseline exit bar. The comparison was against the frozen EMA holdout.

| Missing bars | Median return change | Worst change | Best change | Scenarios worse |
|---:|---:|---:|---:|---:|
| 1 hour | -0.01 percentage points | -0.41 points | +2.57 points | 7/10 |
| 2 hours | approximately 0.00 points | -0.17 points | +2.57 points | 7/10 |
| 4 hours | approximately 0.00 points | -0.19 points | +0.89 points | 6/10 |

This is useful fault-injection evidence, not a full live outage simulation. A real outage
still requires preserving the last known state, blocking actions while data is unavailable,
and reconciling the account after recovery.

## Conclusion

EMA follows the same conditions and variables as SMA, but the historical result does not
resolve the SMA concerns:

- The full formal development set produced no eligible candidate.
- The frozen EMA baseline made money on the holdout but underperformed buy-and-hold by
  48.07 percentage points.
- Cost stress changed the holdout result from positive to negative.
- Chronological consistency was weak: only 2 of 6 expected-cost blocks and 1 of 6
  stressed-cost blocks were profitable.
- Runtime data loss usually changed results only slightly in these samples, but had an
  adverse tail and can alter the future trade path.

EMA therefore does not pass the robustness gate and is not ready for live order execution.
It is available for replay and read-only `live-observe`; EMA live order submission remains
disabled. No EMA demo order, position, journal, or account mutation was created by these
tests.
