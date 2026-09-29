# Contributing

External feedback and reproducible issues are welcome. This repository contains public
reference code, while official starter-kit changes remain maintainer-controlled and require
maintainer review.

Before proposing a change:

1. Keep Developer Platform documentation authoritative; link to it instead of copying it.
2. Use synthetic fixtures and keep tests offline by default.
3. Preserve replay as the default and keep all mutation gates fail closed.
4. Run `python -m ruff check .`, `python -m mypy`, and `python -m pytest`.
5. Do not add credentials, customer data, environment dumps, or real trade artifacts.

The repository is licensed under Apache-2.0. Third-party contributions require maintainer
approval and must be compatible with this license.
