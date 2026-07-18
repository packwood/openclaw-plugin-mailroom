# Changelog

All notable changes to Mailroom will be documented here. The project follows
Semantic Versioning after the first stable release.

## [Unreleased]

### Added

- Self-contained Python workflow engine and SQLite migrations.
- Internal Outlook adapter with portable account and connection configuration.
- Reproducible package verification and GitHub Actions CI.
- Apache-2.0 licensing and contributor, security, and architecture guides.
- Safe empty routing defaults and a synthetic example rulepack.
- Package-relative `openclaw mailroom` commands for scheduled operations.
- Repeatable reconciliation for accepted and uncertain send outcomes.
- ClawHub compatibility and build metadata for `@packwood/mailroom`.

### Changed

- Removed environment-specific imports and defaults from the distributable
  runtime; all deployment identity now comes from plugin configuration.
- Responsibility-profile drift now fails closed when the discovered OpenClaw
  fleet and effective profile set differ.
- Operator-facing cards visibly delimit untrusted email content, and chat-bound
  errors no longer expose internal filesystem or provider details.
