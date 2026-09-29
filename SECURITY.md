# Security

## Credentials

Keep API keys, tokens, usernames, trade keys, passwords, account identifiers, and customer
endpoints out of Git, logs, screenshots, and issues. Secrets are accepted only through
environment variables. The YAML loader rejects common credential field names.

If a credential is exposed, revoke or rotate it through API Key Management before doing
anything else.

## Trading Safety

Replay is the default. Real mutations require both `TRADER_API_ENABLE_LIVE_TRADING=true`
and `--execute`. An unresolved recovery journal blocks new positions. Never delete a
journal merely to bypass recovery; inspect the account in Trader Terminal first.

Do not attach raw API responses, account data, positions, orders, or recovery journals to
an issue. Use synthetic or fully redacted reproduction data.

## Reporting

Do not open a public issue for a suspected credential exposure or exploitable API defect.
Report vulnerabilities privately through [GitHub private vulnerability reporting](
https://github.com/mF-Technologies/trader-api-examples/security/advisories/new).
Do not include credentials, customer data, or unredacted account responses in the report.
