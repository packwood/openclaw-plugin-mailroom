# Contributing

Thank you for improving Mailroom. Email automation can create irreversible
external effects, so changes are reviewed with a safety-first standard.

## Development setup

1. Install Node.js 22.19+, Python 3.9+, and a compatible OpenClaw release.
2. Run `npm ci`.
3. Run `npm run test:all`.
4. Run `npm run verify:package` before opening a pull request.

Tests use temporary SQLite databases and synthetic messages. They must not need
live credentials, production mailboxes, or network access.

## Pull requests

- Keep changes focused and explain the failure mode or capability they address.
- Add a regression test for state transitions, authorization, fingerprinting,
  reconciliation, pagination, concurrency, or error recovery changes.
- Do not commit email bodies, customer names, real mailbox addresses, Telegram
  IDs, connection IDs, API keys, signatures, production rulepacks, or ledgers.
- Document new configuration and preserve fail-closed behavior.
- Call out any migration or deployment-order requirement explicitly.

Maintainers may ask for a shadow-mode validation before approving changes that
touch intake, routing, drafting, or provider adapters.

## Design principles

- Drafting and sending are separate capabilities.
- Unknown send outcomes are never automatically retried.
- Human approvals bind to stored content and exact identities.
- External provider responses are treated as untrusted input.
- Durable state changes precede success acknowledgements.
- Safe defaults send ambiguous work to review.

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
