# Changelog

All notable changes to Mailroom will be documented here. The project follows
Semantic Versioning after the first stable release.

## [Unreleased]

## [0.1.7] - 2026-07-26

### Added

- Configure the agent and Telegram account used for ambiguous routing reviews,
  with Orchestrator-compatible `main` and `default` defaults.

### Fixed

- Prevent already-notified routing reviews and draft approvals from consuming
  the dispatch page limit and permanently starving records that have no
  Telegram card.
- Route Orchestrator-owned approval cards through the configured routing-review
  Telegram account even when Telegram account discovery is unavailable.

## [0.1.6] - 2026-07-24

### Fixed

- Deliver approval cards through the shared review Telegram account when the
  owning agent does not have a dedicated Telegram bot, while preserving the
  owning agent's routing and drafting responsibility.

## [0.1.5] - 2026-07-24

### Added

- Record trusted OpenClaw run, session, and observed tool metadata with each
  proposal for diagnostics without making specific tools an approval gate.
- Require workflow-neutral drafting attestations and show email/calendar check
  status on approval cards.

### Changed

- Delegate email and scheduling workflow selection to each persistent agent's
  current workspace instructions, skills, context, and read-only tools rather
  than loading named skill paths in Mailroom.
- Require fresh, complete calendar evidence and concrete times when the sender
  directly asks for availability.

### Fixed

- Prevent overlapping dispatcher cycles and expired workers from replacing or
  poisoning a newer draft through version-bound drafting leases.
- Revalidate revised and legacy proposals before Outlook draft creation so a
  stale approval card queues a fresh proposal instead of being consumed.

## [0.1.4] - 2026-07-24

### Fixed

- Clean Python bytecode caches and rebuild the runtime after prepack tests so
  packaging remains reproducible across supported Python versions.

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
