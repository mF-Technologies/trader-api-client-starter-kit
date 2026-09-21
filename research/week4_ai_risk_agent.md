# Week 4: bounded AI risk-agent experiment

## Status

The first experiment is implemented as a replay-only treatment beside the deterministic
SMA/EMA control. No live order path was changed, and no live order is authorised by this
experiment.

## Experimental question

Does a bounded account-context risk assistant improve or preserve out-of-sample risk-adjusted
performance when it can choose only `NORMAL`, `REDUCE`, or `PAUSE` at a detected bullish entry?

## Control and treatment

- Control: the existing SMA or EMA replay with the frozen strategy parameters, transaction-cost
  assumptions, and deterministic risk limits.
- Treatment: the same fixture, parameters, costs, and hard limits, plus one AI decision at each
  bullish signal. `NORMAL` keeps the amount, `REDUCE` uses the configured 0.5 amount multiplier,
  and `PAUSE` skips the entry.

The AI cannot alter moving-average periods, ATR stops, exits, position limits, holding time,
daily loss, drawdown, prices, or execution timing. A timeout, malformed response, invalid action,
or API error becomes `PAUSE`.

## Input and data requirements

The replay sends only a sanitized point-in-time context: contract, signal-bar timestamp, close,
fast and slow average, ATR, assumed spread, simulated equity, simulated daily P/L percentage,
simulated drawdown, open-position count, and a market-data-freshness flag. Equity and drawdown
are derived from bars available up to the signal; future bars and future P/L are not sent. No
Trader API key, username, trade key, account fingerprint, or raw credential is sent to the model.

The treatment requires `OPENAI_API_KEY` and `OPENAI_MODEL` in the ignored `.env.local` file. The
model response and action counts are written to the JSON result. The control must be run on the
same fixture before the treatment.

## Safety and feasibility

This is feasible as a research overlay because the existing replay engine remains the final
authority. It is not evidence that the strategy is ready for live trading: external model output
can vary, the simulated account context is not a broker account snapshot, and the historical
cost assumptions remain assumptions. The overlay is explicitly rejected for `live-observe` and
`live-execute`.

## Next experiment

Run control and treatment on the frozen development/holdout and chronological out-of-sample
windows. Record the fixture hash, model, prompt version, costs, limits, action counts, AI failure
count, number of reduced entries, strategy return, Sharpe, drawdown, trade count, and rejected
entries. Treat the AI treatment as acceptable only if it does not violate hard limits and meets
the predeclared selection rule without relying on future information.

## Preliminary Codex-assisted offline replay

This exploratory run used the local H1 fixture
`XAUUSD_H1_200701010300_202609031700.csv`: 63,025 rows from 2007-01-01 03:00 UTC through
2026-09-03 17:00 UTC. The chronological split was index 44,118 (approximately 70% development
and 30% holdout). Frozen settings were SMA20/SMA50, ATR14, buffer `0`, ATR stop multiple `2`,
amount `10`, 120-hour maximum holding time, 1% maximum trade loss, 2% daily loss, 10% drawdown,
commission `0.05` per API amount per side, spread `2.65` bps, slippage `0.5` bps per side,
financing `2` bps/day, and market impact `0.5` bps per side.

For this offline demonstration, Codex applied a fixed drawdown-only policy to the sanitized
signal context: `NORMAL` below 4% drawdown, `REDUCE` from 4% up to but not including 8%, and
`PAUSE` at 8% or higher. `REDUCE` used 50% of the configured amount. This was a manually
reviewed policy injected into replay; it did not call an OpenAI API and must not be described
as an automated model result. The checked-in `--ai-risk-agent` flag remains API-backed; this
offline treatment was run through a local replay harness.

| Window | Treatment | Strategy return | Buy-and-hold return | Sharpe | Max drawdown | Trades | Decisions |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Development | Deterministic control | 6.18% | 81.05% | 0.251 | 10.11% | 18 | — |
| Development | Codex-assisted offline | 8.21% | 81.05% | 0.389 | 8.39% | 18 | 7 normal / 11 reduce / 472 pause |
| Holdout | Deterministic control | 96.60% | 110.29% | 1.955 | 10.10% | 176 | — |
| Holdout | Codex-assisted offline | 83.59% | 110.29% | 2.030 | 8.74% | 178 | 92 normal / 86 reduce / 28 pause |

The offline treatment reduced holdout return by about 13 percentage points compared with the
control, while slightly improving Sharpe and reducing maximum drawdown. It therefore does not
show a clear performance improvement. Because the thresholds were selected for this exploratory
demonstration rather than predeclared before development, these results are descriptive only and
do not pass the project's robustness or model-selection gate.

## Self-locking pause diagnosis and recovery test

The development pause count is largely explained by a feedback loop in the exploratory policy.
The first development `PAUSE` decision occurred on 26 April 2016. Once drawdown reached the
soft 8% threshold, the policy stopped opening positions. Because the account then held no new
positions, its simulated equity could not recover, so the drawdown remained above 8% and later
entry signals were paused as well. The development window produced 7 `NORMAL`, 11 `REDUCE`, and
472 `PAUSE` decisions. The holdout replay started with a fresh simulated equity high-water mark;
it did not reach the soft pause threshold until April 2026, producing 92 `NORMAL`, 86 `REDUCE`,
and 28 `PAUSE` decisions.

As a diagnostic, we tested a recovery-aware policy that paused once at 8%, waited for a fixed
24-, 72-, or 120-hour H1 cooldown, and then allowed only half-size entries while the hard 10%
drawdown limit had not been reached. This reduced repeated pauses, but it caused the hard
drawdown limit to trigger in both windows. The results were:

| Cooldown | Window | Strategy return | Sharpe | Max drawdown | Trades | Hard drawdown triggered |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 24 hours | Development | 6.28% | 0.299 | 10.03% | 23 | Yes |
| 24 hours | Holdout | 80.80% | 1.973 | 10.13% | 181 | Yes |
| 72 hours | Development | 6.28% | 0.299 | 10.03% | 23 | Yes |
| 72 hours | Holdout | 80.80% | 1.973 | 10.13% | 181 | Yes |
| 120 hours | Development | 6.28% | 0.299 | 10.03% | 23 | Yes |
| 120 hours | Holdout | 81.03% | 1.981 | 10.02% | 180 | Yes |

This recovery variant is rejected for now. A pause that requires review is safer than silently
restarting at elevated drawdown. Any future automatic recovery rule must be predeclared, tested
with a smaller recovery size or stricter conditions, and kept separate from the hard drawdown
limit. The original sticky-pause result remains the primary safety evidence.

## Revised recommended design: pause episodes and constrained recovery probes

To test a practical way to alleviate the self-locking behavior, the next replay used a stateful
policy with the following predeclared research rules:

1. A drawdown of 8% starts one pause episode and a 24-hour H1 cooldown.
2. The high-water mark is never reset.
3. A hard 10% drawdown remains authoritative and blocks further entries.
4. After the cooldown, drawdown below 6% restores normal mode.
5. If drawdown remains between 6% and 10%, the policy permits only a small recovery probe.
6. Two recovery sizes were tested: 10% and 25% of the normal amount.

The same fixture, costs, limits, and development/holdout split were used. The results were:

| Recovery probe | Window | Strategy return | Sharpe | Max drawdown | Trades | Pause decisions | Hard drawdown triggered |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 10% | Development | 7.32% | 0.329 | 9.14% | 488 | 2 | No |
| 10% | Holdout | 27.17% | 1.253 | 8.54% | 205 | 1 | No |
| 25% | Development | 6.30% | 0.305 | 10.01% | 123 | 1 | Yes |
| 25% | Holdout | 65.56% | 1.820 | 10.02% | 190 | 2 | Yes |

The 10% recovery probe alleviated the repeated-lock behavior without reaching the hard drawdown
limit, but it produced much lower returns than the deterministic control and the original
sticky-pause treatment. It also created many small reduced-size trades, so its trade count is not
directly comparable without considering position size. The 25% probe allowed more exposure but
still reached the hard drawdown limit and is rejected.

This revised design is therefore a safer research candidate than the 25% or 50% recovery rules,
but it is not selected for live use and does not demonstrate that the risk agent improves the
strategy. The 10% thresholds and probe size were explored after the original result, so these
figures remain descriptive rather than a valid frozen model-selection result. A production
implementation would also need a persistent pause-episode state, an explicit manual-reset path,
and separate tests for restart behavior before any automatic recovery could be considered.
