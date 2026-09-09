# H1 SMA Strategy Validation

Date: 2026-09-08

## Purpose

This validation checks whether the frozen long-only SMA20/SMA50 strategy behaves
consistently across different chronological parts of the available hourly history.
No parameters were retuned for any validation block.

## Data and protocol

- Instrument mapping: account contract LLG; uploaded file labelled XAUUSD.
- Data: 62,851 contiguous hourly bars from 2016-01-18 02:00 UTC to 2026-09-03 17:00 UTC.
- Split: six sequential blocks of approximately 10,475 bars each.
- Each block starts flat. Bars before a block are available only for indicator warm-up.
- Signals use completed bars and execute on the next hourly bar.
- Frozen strategy: SMA20/SMA50, ATR14, zero exit buffer, ATR stop multiple 2.
- Frozen controls: amount 10, one position, 120-hour maximum holding time, 1% maximum
  trade loss, 2% daily loss, and 10% maximum drawdown.
- Annualisation: 6,048 hourly observations per year.

The expected cost case uses commission 0.05 per API amount per side, 2.65 bps total
spread, 0.5 bps slippage per side, 2 bps/day financing, and 0.5 bps market impact per
side. The stressed case uses 0.10 commission per API amount per side, 4 bps spread,
1 bps slippage per side, 5 bps/day financing, and 2 bps market impact per side.
Financing and market impact are still sensitivity assumptions, not confirmed broker terms.

## Expected-cost results

| Block | Period | Strategy return | Buy-and-hold | Excess return | Sharpe | Max drawdown | Trades |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | 2016-01-18 to 2017-10-24 | -0.95% | 4.49% | -5.45% | -0.06 | 10.14% | 61 |
| 2 | 2017-10-24 to 2019-08-02 | -9.72% | -0.05% | -9.67% | -1.86 | 10.10% | 46 |
| 3 | 2019-08-02 to 2021-05-13 | 17.30% | 12.40% | 4.90% | 0.94 | 6.67% | 119 |
| 4 | 2021-05-13 to 2023-02-17 | -7.55% | -11.47% | 3.92% | -1.04 | 10.02% | 54 |
| 5 | 2023-02-17 to 2024-11-25 | -6.33% | 32.16% | -38.49% | -0.79 | 10.11% | 29 |
| 6 | 2024-11-25 to 2026-09-03 | 54.91% | 54.73% | 0.18% | 2.33 | 10.46% | 85 |

The strategy was profitable in 2 of 6 blocks. Multiplying the independent block
returns gives approximately 40.71%, but this is a descriptive compounded figure rather
than a continuous account equity curve. The median block return was -0.95% and the
worst block returned -9.72%.

## Stressed-cost results

| Block | Strategy return | Buy-and-hold | Excess return | Sharpe | Max drawdown | Trades |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | -1.70% | -14.95% | 13.24% | -0.12 | 10.30% | 51 |
| 2 | -10.02% | -19.54% | 9.52% | -2.12 | 10.10% | 34 |
| 3 | -9.02% | -7.18% | -1.84% | -1.36 | 10.28% | 32 |
| 4 | -7.86% | -30.90% | 23.04% | -1.30 | 10.10% | 38 |
| 5 | -6.75% | 12.68% | -19.43% | -0.89 | 10.01% | 22 |
| 6 | 43.51% | 35.23% | 8.28% | 1.83 | 10.48% | 84 |

The strategy was profitable in 1 of 6 blocks. The descriptive compounded result was
approximately -0.78%, with a median block return of -6.75% and a worst block return of
-10.02%.

## Conclusion

The frozen strategy does not pass a robustness validation gate. It performs well in the
most recent block and in one earlier block, but the expected-cost case loses money in four
of six chronological blocks. The stressed-cost case is mostly negative. The 10% drawdown
limit constrains observed drawdown, so it should not be interpreted as evidence that the
underlying strategy naturally maintains a 10% drawdown.

The result supports continued paper/live-observe testing, not live trading. The next
validation evidence should be a sustained live-observe run, full duplicate-signal and
restart/reconciliation tests, and a later re-run after actual commission, financing, and
execution-impact measurements are available.
