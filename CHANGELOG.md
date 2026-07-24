# Changelog

All notable changes to Mailroom will be documented here. The project follows
Semantic Versioning after the first stable release.

## [Unreleased]

## [0.1.3] - 2026-07-24

### Fixed

- Publish the exact locally built and verified npm-pack tarball to ClawHub so
  the release includes the declared `dist/index.js` runtime entry.

## [0.1.2] - 2026-07-19

### Changed

- Renamed the plugin display name from `Mailroom Approvals` to `Mailroom` to
  reflect its broader responsibility-profile, routing, drafting, and approval
  workflow.

## [0.1.1] - 2026-07-18

### Changed

- Expanded the GitHub and ClawHub description to explain automatic fleet
  profiling, scheduled profile evolution, smart routing, context-rich drafting,
  and button-based approval workflows.
- Added a packaged Mermaid workflow diagram and rendered SVG for GitHub and
  ClawHub documentation.

## [0.1.0] - 2026-07-18

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
