# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| `0.2.x` | Yes       |
| `< 0.2` | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in **SelfHealAPI**, please report it privately rather than opening a public GitHub issue:

1. Use **GitHub Private Vulnerability Reporting** (`Security` tab -> `Report a vulnerability`) on `creova-gif/selfheal-api`.
2. Include a clear description, affected module or endpoint, reproduction steps, and potential impact.

We aim to acknowledge reports within **48 hours** and provide a remediation timeline or patch once verified.

## Security Architecture & Controls

SelfHealAPI is engineered with security-by-design and privacy-by-design:

- **Zero-PII Boundary:** Incoming payload scalars are redacted into structural primitives (`<str>`, `<int>`, `<int_str>`, `<float_str>`, `<bool_str>`, `<iso_datetime>`, `<null>`) before LLM mapping or SQLite ledger persistence. Identifier-like object keys (digit-only IDs, UUIDs, emails) are filtered prior to LLM fallback.
- **Zero-`exec()` Runtime Normalization:** In-memory drift healing executes pure-Python coercion and field mapping without runtime `exec()` or `eval()`.
- **Multi-Tenant Isolation:** Hosted endpoints enforce HMAC-SHA256 webhook verification (`X-Hub-Signature-256` / `X-SelfHeal-Signature`), per-tenant Bearer/HMAC read authorization, pinned tenant protection, and bounded request body limits (`1 MiB` default).
- **SSRF Protection:** Outbound Slack and webhook alert notifiers enforce `https://` and block `localhost`, `.local`, `.internal`, loopback, private, and link-local metadata IP ranges (such as `169.254.169.254`) by default.
- **XSS & Injection Defense:** The Drift Console enforces strict HTML escaping, `https://`/`http://` PR link validation, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`. CSV exports neutralize spreadsheet formula injection (`=`, `+`, `-`, `@`, `\t`, `\r`).
- **Least-Privilege Runtime:** Container images run as non-root `appuser` (`uid 10001`) with `/readyz` readiness checks and SQLite WAL persistence.
