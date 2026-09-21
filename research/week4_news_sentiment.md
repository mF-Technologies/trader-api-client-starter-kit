# Week 4: news and sentiment experiment

## Status

The sentiment experiment is designed but not yet run as a historical performance test. The
repository contains historical OHLC price fixtures but no timestamped news or sentiment feed.
Price data alone cannot reconstruct what information was available at each historical decision.
Using current articles against old bars would introduce future-information leakage, so no
sentiment return result is claimed yet.

## Why AI is appropriate here

News is unstructured text. A model can help extract whether an article is relevant to gold,
identify its event type, describe the likely direction and uncertainty, and explain the result in
plain language. These tasks are harder to express reliably with simple price rules.

The AI must not directly place orders or override the deterministic risk controls. The intended
flow is:

`timestamped article -> AI structured assessment -> deterministic policy -> strategy review`

## Required input data

The historical news file should contain at least:

```text
published_at_utc, headline, source, url
```

Article body text is useful but not required. Publication time must be the time at which the
article became available, not the time it was downloaded. Duplicate articles and later updates
must be identified, and the feed must cover the same period and timezone as the H1 price bars.

## Proposed AI output

For each article, the AI would return structured fields:

- `relevance`: `RELEVANT`, `NOT_RELEVANT`, or `UNCERTAIN`;
- `sentiment`: `BULLISH`, `BEARISH`, `NEUTRAL`, or `UNCERTAIN` for gold;
- `impact`: `LOW`, `MEDIUM`, or `HIGH`;
- `event_type`: for example rates, inflation, central bank, USD, geopolitics, or supply;
- `confidence`: a number from 0 to 1;
- `reason`: a short explanation tied to the article text;
- `source_quality`: an assessment of source reliability, separate from sentiment.

The model would not be allowed to return a price target, change an SMA/ATR parameter, change a
stop, or submit an order.

## Planned control and treatment

- Control: the existing SMA strategy with no news input.
- Treatment: the same strategy and all the same costs and hard limits, with the timestamped AI
  assessments available to a bounded news policy.

Before holdout testing, one news policy must be frozen. A candidate policy for development
testing is to block a new entry for six H1 bars only when at least two independent, high-quality,
high-impact articles in the preceding six hours are both relevant and confidently bearish. All
other assessments remain advisory. This candidate is not approved or selected yet; it must be
declared before the formal development/holdout run.

## Evaluation metrics

The treatment will be compared with the control using strategy return, Sharpe, maximum drawdown,
trade count, blocked entries, holding time, and transaction costs. It will also record sentiment
coverage, duplicate handling, model failures, stale or missing articles, source-quality reasons,
and whether each block was caused by the news policy or an existing hard risk limit.

The central question is not whether the AI can sound persuasive. It is whether timestamped news
adds useful information out of sample without increasing drawdown, costs, or unexplained blocked
trades.

## Current limitation and next step

No historical news fixture is currently available, and no OpenAI API call has been made. The next
required input is a timestamped news CSV or export covering part of the H1 history. Once supplied,
Codex can classify the articles offline, produce explanations, run the control/treatment replay,
and document both positive and rejected results.
