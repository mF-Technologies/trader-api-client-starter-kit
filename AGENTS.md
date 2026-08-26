# Repository Instructions

This repository contains runnable examples, not duplicated API documentation. Treat the
Developer Platform contracts referenced by `compatibility.yaml` as upstream inputs.

- Use Python 3.12 and typed, small modules under `src/trader_api_examples/`.
- Test public CLI, config, API-client, strategy, and journal boundaries with offline fixtures.
- Keep replay as the default. Never weaken or bypass live mutation gates.
- Never commit credentials, account data, customer endpoints, or live trade artifacts.
- Treat live API observations as time-specific evidence, not permanent contract proof.
- Run Ruff, mypy, and pytest before delivery.
