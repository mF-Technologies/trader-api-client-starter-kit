# Week 4: account-context monitoring experiment

## Purpose

This experiment tests whether an account-context monitor can identify and explain elevated
risk without changing the strategy or submitting orders. The monitor runs in shadow mode: its
status is recorded, but the deterministic SMA replay remains in control of all entries, exits,
position sizing, and hard limits.

The AI role is explanation and triage. Codex interprets the sanitized monitor snapshots and
explains why a snapshot is `NORMAL`, `CAUTION`, or `PAUSE_REVIEW`. It does not change thresholds,
reset drawdown, select parameters, or place orders. No OpenAI API call was made; this was a
Codex-assisted offline explanation experiment.

## Monitoring bands

The bands were declared for this exploratory run:

- `NORMAL`: drawdown below 4%, daily P/L above -1%, and fresh market data.
- `CAUTION`: drawdown from 4% to below 8%, daily P/L from -2% to -1%, or another elevated
  context that should be reviewed.
- `PAUSE_REVIEW`: drawdown at or above 8%, daily P/L at or below -2%, or stale market data.

The existing hard 10% drawdown limit, 2% daily-loss limit, position limit, holding-time limit,
and protective stop remain authoritative. These monitoring bands are alerts; they are not a
replacement for the deterministic controls.

## Data and procedure

The experiment used the H1 fixture
`XAUUSD_H1_200701010300_202609031700.csv`, containing 63,025 bars from 2007-01-01 through
2026-09-03. The same approximately 70/30 chronological split was used as the earlier replay:

- Development: indexes `[0, 44118)`, 2007-01-01 through 2023-06-23.
- Holdout: indexes `[44118, 63025)`, 2023-06-23 through 2026-09-03.

The replay used SMA20/SMA50, ATR14, zero exit buffer, ATR stop multiple 2, amount 10,
120-hour maximum holding time, 1% maximum trade loss, 2% maximum daily loss, 10% maximum
drawdown, and the expected transaction-cost assumptions. Snapshots were collected at bullish
entry opportunities. The monitor returned a shadow `NORMAL` decision to the replay so that
strategy performance could be compared with the existing deterministic control.

## Results

The shadow monitor did not change trading results:

| Window | Strategy return | Sharpe | Max drawdown | Trades |
| --- | ---: | ---: | ---: | ---: |
| Development | 6.18% | 0.251 | 10.11% | 18 |
| Holdout | 96.60% | 1.955 | 10.10% | 176 |

The account-context status counts were:

| Window | Normal | Caution | Pause/review | Snapshots |
| --- | ---: | ---: | ---: | ---: |
| Development | 8 | 8 | 2 | 18 |
| Holdout | 105 | 60 | 11 | 176 |

The first development `PAUSE_REVIEW` snapshot occurred on 2016-04-07 at a simulated 8.21%
drawdown. The monitored drawdown reached 9.18% in the reviewed snapshots. The first holdout
`PAUSE_REVIEW` snapshot occurred on 2024-06-25 at a simulated 8.30% drawdown, with a reviewed
maximum of 9.19%.

Daily P/L did not drive these alerts in the sampled snapshots: development values ranged from
0.00% to 0.00%, and holdout values ranged from -0.45% to 0.62%. Market data was marked fresh
throughout the replay, and the spread was fixed at the assumed 2.65 bps, so neither data-quality
nor spread alerts were exercised by this fixture.

## Codex explanations

### Development

The account was mostly normal early in the sample, then moved into caution and pause/review as
drawdown increased in 2016. The appropriate explanation is that the account had lost distance
from its equity high-water mark; a review of exposure and strategy behavior was warranted before
adding risk. The monitor did not claim that the next price move was predictable and did not
change the strategy's parameters.https://www.msn.com/zh-hk/%E8%97%9D%E8%A1%93/%E8%A6%96%E8%A6%BA%E8%97%9D%E8%A1%93%E8%88%87%E8%A8%AD%E8%A8%88/%E7%BE%85%E6%B5%AE%E5%AE%AE%E8%92%99%E5%A8%9C%E9%BA%97%E8%8E%8E%E5%B1%95%E9%96%93%E8%87%AA%E8%B2%BC%E7%95%AB%E4%BD%9C-%E5%BE%B7%E5%9C%8B%E5%85%A9%E7%94%B7%E6%AF%80%E6%90%8D%E8%A2%AB%E6%8D%95/ar-AA2cB1bd

### Holdout

The holdout contained many normal snapshots and a long profitable period, but it still produced
periodic caution and pause/review alerts. A strong total return does not remove account risk:
the monitor correctly highlighted periods in which drawdown was elevated. The appropriate action
would be to review the account and data state, not to assume that the strategy should be stopped
or that its parameters should be changed automatically.

## Conclusion

Account-context monitoring is a more suitable AI experiment than asking AI to calculate SMA or
ATR rules. The deterministic monitor found elevated drawdown periods and Codex provided plain
language explanations while leaving execution unchanged. This demonstrates useful advisory and
reporting value, but it does not prove that AI improves returns.

The experiment is limited because snapshots were collected at entry opportunities rather than
from a continuous broker-account stream, the equity and P/L were simulated, and the fixture did
not contain stale-data or changing-spread failures. A future implementation could poll the
read-only account-inspector and market-data-monitor commands continuously, persist snapshots,
and let AI explain them. Deterministic limits must remain authoritative, and any automatic
pause or recovery action would require a separate predeclared test.
