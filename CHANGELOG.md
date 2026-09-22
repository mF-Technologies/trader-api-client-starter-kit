# Changelog

## 0.1.0 - Unreleased

- Establish five execution-focused Trader API reference tools.
- Add TA-Lib RSI replay, observation, guarded execution, and recovery boundaries.
- Add offline tests, compatibility metadata, security guidance, and CI gates.
- Load local credentials from an ignored `.env.local` file beside the selected config.
- Add a shared process-scoped Price Agent session with fail-closed health handling.
- Add the `algo-runner` flow with RSI, MACD, EMA Cross, multi-instance execution,
  heartbeat reporting, and separate recovery journals.
- Add rotating per-instance JSONL logs for strategy evaluation and execution lifecycle
  diagnostics without credentials or full price tags.
- Pause and retry only the algo instance affected by stale or insufficient ChartServer data,
  automatically recover it, and close its owned position only after a configurable grace
  period.
- Reuse persisted cleanup idempotency keys when liquidation confirmation requires recovery.
- Convert the price client's unhandled background disconnect task into a fail-closed transport
  error instead of emitting an unretrieved-task traceback.
