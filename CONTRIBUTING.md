# Contributing

External feedback and reproducible issues are welcome during the private phase. Official
example code remains maintainer-controlled.

Before proposing a change:

1. Keep Developer Platform documentation authoritative; link to it instead of copying it.
2. Use synthetic fixtures and keep tests offline by default.
3. Preserve replay as the default and keep all mutation gates fail closed.
4. Run `python -m ruff check .`, `python -m mypy`, and `python -m pytest`.
5. Do not add credentials, customer data, environment dumps, or real trade artifacts.

Public release, licensing, and third-party contribution terms require a separate human
legal and product approval.
