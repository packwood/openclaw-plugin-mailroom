import unittest

from mailroom.drafting_policy import DraftingPolicy


class DraftQualityGateTests(unittest.TestCase):
    def test_loads_without_an_external_skill(self):
        policy = DraftingPolicy.load()
        self.assertEqual(policy.version, 2)
        self.assertEqual(policy.name, "mailroom-draft-quality")
        self.assertEqual(
            policy.validators,
            frozenset({"opening-em-dash", "recipient-first-name"}),
        )

    def test_detects_opening_em_dash_and_unsupported_name(self):
        policy = DraftingPolicy.load()
        violations = policy.violations(
            {"reply_text": "David — thanks for sending this."}, sender_name="Kate Faust",
        )
        self.assertEqual(len(violations), 2)
        self.assertTrue(any(value.startswith("opening-em-dash") for value in violations))
        self.assertTrue(any(value.startswith("recipient-first-name") for value in violations))

    def test_accepts_supported_sender_name_and_non_name_opening(self):
        policy = DraftingPolicy.load()
        self.assertEqual(
            policy.violations(
                {"reply_text": "Kate, thanks for sending this."},
                sender_name="Kate Faust (Rockwood)",
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
        policy = DraftingPolicy.load()
        for reply in ("...", "…", "TBD", "placeholder", "---"):
            with self.subTest(reply=reply):
                self.assertIn(
                    "placeholder-reply-text",
                    " ".join(policy.violations(
                        {"reply_text": reply}, sender_name="Ethan Sands",
                    )),
                )

    def test_detects_named_salutation_variants(self):
        policy = DraftingPolicy.load()
        for opening in ("Hi David!", "Good morning, David,", "Hey David,", "David - thanks"):
            with self.subTest(opening=opening):
                violations = policy.violations(
                    {"reply_text": opening}, sender_name="Kate Faust",
                )
                self.assertTrue(any(value.startswith("recipient-first-name") for value in violations))

    def test_stored_proposal_revalidates_content_without_skill_audit(self):
        policy = DraftingPolicy.load()
        proposal = {"reply_text": "Kate, thanks."}
        self.assertEqual(
            policy.stored_proposal_violations(proposal, sender_name="Kate Faust"), [],
        )
        self.assertTrue(any(
            value.startswith("recipient-first-name")
            for value in policy.stored_proposal_violations(
                {"reply_text": "David, thanks."}, sender_name="Kate Faust",
            )
        ))


if __name__ == "__main__":
    unittest.main()
