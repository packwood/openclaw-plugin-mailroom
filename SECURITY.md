# Security policy

## Reporting a vulnerability

Please use GitHub's private security-advisory feature for vulnerabilities. Do
not open a public issue for bugs that could send email without approval, bypass
callback authorization, expose message content or credentials, corrupt the
ledger, or cause unsafe replay after a partial failure.

Include the affected version, configuration, reproduction steps using synthetic
data, and the expected versus observed state transition. Do not attach a
production database, secret file, connection registry, signature, or email.

## Supported versions

Until 1.0, only the latest tagged release receives security fixes.

## Operator responsibilities

- Treat every OpenClaw plugin as trusted local code.
- Restrict the plugin allowlist and filesystem permissions.
- Keep `MATON_API_KEY`, model-provider keys, and connection metadata out of the
  repository and readable only by the OpenClaw service account.
- Back up the SQLite ledger before upgrades.
- Validate changes in shadow mode, then with one manual production cycle.
- Never publish production rulepacks or diagnostic output containing mail.
