from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mailroom.drafting_policy import DraftPolicyError, DraftingPolicy


SKILL = """---
name: email-drafting
description: Test
---

## 5. Voice & the non-negotiables

<!-- mailroom-policy-version: 1 -->
<!-- mailroom-validator: opening-em-dash -->
- Never use an em dash in an opening.
<!-- mailroom-validator: recipient-first-name -->
- Never guess a recipient's first name.

## 6. The loop

Draft and review.
"""


class DraftingPolicyTests(unittest.TestCase):
    def test_loads_contract_and_hashes_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "SKILL.md"
            path.write_text(SKILL)
            policy = DraftingPolicy.load(path)
        self.assertEqual(policy.version, 1)
        self.assertEqual(len(policy.skill_sha256), 64)
        self.assertIn("Never use an em dash", policy.contract)
        self.assertNotIn("mailroom-validator", policy.contract)
        self.assertNotIn("Draft and review", policy.contract)

    def test_rejects_missing_required_validator(self):
        with self.assertRaisesRegex(DraftPolicyError, "recipient-first-name"):
            DraftingPolicy.from_text(
                SKILL.replace("<!-- mailroom-validator: recipient-first-name -->", ""),
            )

    def test_detects_opening_em_dash_and_unsupported_name(self):
        policy = DraftingPolicy.from_text(SKILL)
        violations = policy.violations(
            {"reply_text": "David — thanks for sending this."}, sender_name="Kate Faust",
        )
        self.assertEqual(len(violations), 2)
        self.assertTrue(any(value.startswith("opening-em-dash") for value in violations))
        self.assertTrue(any(value.startswith("recipient-first-name") for value in violations))

    def test_accepts_supported_sender_name_and_non_name_opening(self):
        policy = DraftingPolicy.from_text(SKILL)
        self.assertEqual(
            policy.violations(
                {"reply_text": "Kate, thanks for sending this."}, sender_name="Kate Faust (Rockwood)",
            ),
            [],
        )
        self.assertEqual(
            policy.violations(
                {"reply_text": "Thanks, I will review this."}, sender_name="Kate Faust",
            ),
            [],
        )

    def test_rejects_placeholder_reply_text(self):
        policy = DraftingPolicy.from_text(SKILL)
        for reply in ("...", "…", "TBD", "placeholder", "---"):
            with self.subTest(reply=reply):
                self.assertIn(
                    "placeholder-reply-text",
                    " ".join(policy.violations(
                        {"reply_text": reply}, sender_name="Ethan Sands",
                    )),
                )

    def test_detects_named_salutation_variants(self):
        policy = DraftingPolicy.from_text(SKILL)
        for opening in ("Hi David!", "Good morning, David,", "Hey David,", "David - thanks"):
            with self.subTest(opening=opening):
                violations = policy.violations(
                    {"reply_text": opening}, sender_name="Kate Faust",
                )
                self.assertTrue(any(value.startswith("recipient-first-name") for value in violations))

    def test_stored_proposal_requires_current_policy_audit(self):
        policy = DraftingPolicy.from_text(SKILL)
        proposal = {"reply_text": "Kate, thanks."}
        self.assertTrue(any(
            value.startswith("policy-audit-missing")
            for value in policy.stored_proposal_violations(proposal, sender_name="Kate Faust")
        ))
        proposal["_mailroom_policy"] = policy.audit_metadata(
            attempts=1, corrected_violations=[],
        )
        self.assertEqual(
            policy.stored_proposal_violations(proposal, sender_name="Kate Faust"), [],
        )
        proposal["_mailroom_policy"]["skill_sha256"] = "stale"
        self.assertTrue(any(
            value.startswith("policy-audit-stale")
            for value in policy.stored_proposal_violations(proposal, sender_name="Kate Faust")
        ))


if __name__ == "__main__":
    unittest.main()
