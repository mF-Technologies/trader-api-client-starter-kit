# H1 Runtime Data-Loss Validation

Date: 2026-09-09

## Purpose

This test checks how the frozen H1 strategy behaves when completed bars are
temporarily unavailable while a position is open. No live orders or API mutations
were used.

## Historical protocol

- Data: 62,851 normalized H1 bars from 2016-01-18 02:00 UTC to 2026-09-03 17:00 UTC.
- Evaluation window: the existing 30% holdout, starting at index 43,995 on 2023-06-28 05:00 UTC.
- Baseline: SMA20/SMA50, ATR14, zero buffer, ATR stop multiple 2, amount 10.
- Controls: one position, 120-hour maximum holding time, 1% trade-loss limit,
  2% daily-loss limit, and 10% maximum drawdown.
- Expected costs: 0.05 commission per API amount per side, 2.65 bps spread,
  0.5 bps slippage per side, 2 bps/day financing, and 0.5 bps market impact per side.
- Outages: 10 scenarios for each duration. Each outage removed consecutive H1 bars
  ending at a baseline trade exit bar, so the simulated outage occurred while exposed.

The baseline holdout result was 98.98% strategy return, 110.86% buy-and-hold return,
10.04% maximum drawdown, and 175 trades.

## Results

| Missing bars | Median return change | Worst return change | Best return change | Scenarios worse | Trade-count change |
|---:|---:|---:|---:|---:|---:|
| 1 hour | -0.07 percentage points | -1.89 points | +0.51 points | 6/10 | -1 to 0 |
| 2 hours | -0.01 points | -2.33 points | +1.95 points | 7/10 | 0 to +1 |
| 4 hours | +0.20 points | -102.01 points | +3.16 points | 3/10 | -107 to +1 |

The four-hour worst case removed a bar containing a protective-stop exit. The replay
then followed a materially different signal path and produced 107 fewer trades. This
is a warning that a data blackout can change future signals, not merely delay one fill.
The positive median for the four-hour sample is not reassuring because it is driven by
path dependence and a small sample, while the adverse tail is very large.

## Runtime guard checks

- Empty completed-bar response: `INCONCLUSIVE`, stale or missing data.
- Latest H1 bar 3 hours old: `INCONCLUSIVE`, stale or missing data.
- Fresh flat data with a 0.01-second maximum runtime: `NO_SIGNAL`, runtime ended without
  submitting an order.
- A fresh latest bar with an internal four-hour hole: the session-aware health check
  returns `INCONCLUSIVE` with `UNEXPECTED_INTERNAL_GAP`.

## Interpretation and limitation

The runtime guard now handles missing or stale latest data and the configured maximum
runtime. It allows the observed daily break, weekend closure, and configured full-day
holiday dates. Other internal gaps fail closed and are reported as `INCONCLUSIVE` with
the gap endpoints and missing-bar count.

Removing bars from a historical fixture is a useful fault-injection test, but it is
not a complete simulation of a live outage. A production-quality test should preserve
the last known bar, block signal and stop processing during the outage, then reconcile
the account and process the first available bar after recovery. The remaining evidence
should verify the configured session schedule against the account and exercise
restart/reconciliation while a position is open.
