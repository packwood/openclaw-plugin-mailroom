# Routing rulepacks

Mailroom intentionally ships with no active routing rules. With an empty
directory, messages that are not deterministic noise enter routing review.

Copy the examples from `examples/rulepacks/` into a private directory and pass
that directory with `--rulepacks`. Rulepacks may contain customer names,
mailboxes, and business terms; do not commit a production rulepack unless every
value is safe to publish.
