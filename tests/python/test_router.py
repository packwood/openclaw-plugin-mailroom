from __future__ import annotations

import unittest
from pathlib import Path

from mailroom.models import IncomingMessage, Priority
from mailroom.router import DeterministicRouter, RulePack


class RouterTests(unittest.TestCase):
    def test_fleet_rulepacks_route_primary_and_research(self):
        router = DeterministicRouter.from_directory(Path(__file__).parents[1] / "rulepacks")
        primary = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="primary",
            conversation_id="primary", received_at=None, sender_email="investor@example.com",
            sender_name="Investor", subject="Project Redwood follow-up", body_preview="Can we discuss?",
        )
        research = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="research",
            conversation_id="research", received_at=None, sender_email="investor@example.com",
            sender_name="Investor", subject="Re: Example Research — Data Room", body_preview="Received, thanks.",
        )
        self.assertEqual(router.route(primary).draft_owner, "primary")
        self.assertEqual(router.route(research).draft_owner, "research")

    def test_research_routes_known_principal_but_ignores_calendar_acceptance(self):
        router = DeterministicRouter.from_directory(Path(__file__).parents[1] / "rulepacks")
        direct = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="direct",
            conversation_id="direct", received_at=None, sender_email="principal@customer.example",
            sender_name="Customer", subject="Monday timing", body_preview="Can we move our call?",
        )
        accepted = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="accepted",
            conversation_id="accepted", received_at=None, sender_email="principal@customer.example",
            sender_name="Customer", subject="Accepted: Example Project Weekly",
            body_preview="Example Project Weekly",
            raw={"@odata.type": "#microsoft.graph.eventMessageResponse"},
        )
        self.assertEqual(router.route(direct).draft_owner, "research")
        self.assertIsNone(router.route(accepted).draft_owner)
        self.assertEqual(router.route(accepted).outcome, "DROPPED")

    def test_declined_invite_is_dropped_globally_not_sent_to_review(self):
        router = DeterministicRouter([
            RulePack(agent="legal", subject_terms=("project blue",)),
        ])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="declined",
            conversation_id="declined", received_at=None,
            sender_email="person@example.com", sender_name="Person",
            subject="Declined: Project Blue Financing Discussion",
            body_preview="Confidentiality footer",
            raw={"meetingMessageType": "meetingDeclined"},
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "DROPPED")
        self.assertEqual(decision.disposition.value, "noise")

    def test_declined_invite_disclaimer_fallback_handles_erased_graph_type(self):
        router = DeterministicRouter([])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="declined-base",
            conversation_id="declined-base", received_at=None,
            sender_email="person@example.com", sender_name="Person",
            subject="Declined: Project Blue Financing Discussion",
            body_preview=(
                "This message contains PRIVILEGED AND CONFIDENTIAL INFORMATION "
                "intended solely for the use of the addressee."
            ),
            raw={"@odata.type": "#microsoft.graph.message"},
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "DROPPED")
        self.assertIn("global-noise:calendar-response-subject:declined:", decision.reasons)

    def test_human_accepted_terms_email_is_not_hard_dropped(self):
        router = DeterministicRouter([
            RulePack(agent="legal", subject_terms=("revised terms",)),
        ])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="accepted-terms",
            conversation_id="accepted-terms", received_at=None,
            sender_email="lender@example.com", sender_name="Lender",
            subject="Accepted: revised terms",
            body_preview="We accept the revised terms. Please send the final documents.",
            raw={"@odata.type": "#microsoft.graph.message"},
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "ROUTED")
        self.assertEqual(decision.draft_owner, "legal")

    def test_primary_does_not_route_delivery_failures(self):
        router = DeterministicRouter.from_directory(Path(__file__).parents[1] / "rulepacks")
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="bounce",
            conversation_id="bounce", received_at=None, sender_email="postmaster@example.com",
            sender_name="Postmaster", subject="Undeliverable: Project Redwood",
            body_preview="Delivery failed.",
        )
        self.assertIsNone(router.route(message).draft_owner)
        self.assertEqual(router.route(message).outcome, "DROPPED")

    def test_primary_does_not_route_automated_meeting_recaps(self):
        router = DeterministicRouter.from_directory(Path(__file__).parents[1] / "rulepacks")
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="recap",
            conversation_id="recap", received_at=None, sender_email="fred@fireflies.ai",
            sender_name="Fred", subject="Your meeting recap - Project Redwood",
            body_preview="Project Redwood discussion recap.",
        )
        self.assertIsNone(router.route(message).draft_owner)

    def test_selects_one_owner_and_preserves_watchers(self):
        router = DeterministicRouter([
            RulePack(agent="primary", subject_terms=("redwood",), body_terms=("operational services",)),
            RulePack(
                agent="legal", sender_domains=("example.com",),
                subject_terms=("nda",), body_terms=("redline",), watcher_threshold=10,
            ),
        ])
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="1",
            conversation_id="c", received_at=None, sender_email="x@example.com",
            sender_name="X", subject="Project Redwood NDA",
            body_preview="Includes a redline for the operational services transaction",
        )
        decision = router.route(msg)
        self.assertEqual(decision.draft_owner, "legal")
        self.assertEqual(decision.watchers, ("primary",))
        self.assertEqual(decision.priority, Priority.P2)
        self.assertEqual(decision.outcome, "ROUTED")

    def test_critical_term_escalates_priority(self):
        router = DeterministicRouter([
            RulePack(agent="legal", critical_terms=("executed nda",), threshold=30),
        ])
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="1",
            conversation_id="c", received_at=None, sender_email="x@example.com",
            sender_name="X", subject="Executed NDA", body_preview="Attached",
        )
        decision = router.route(msg)
        self.assertEqual(decision.draft_owner, "legal")
        self.assertEqual(decision.priority, Priority.P0)

    def test_short_acronym_does_not_match_inside_monday(self):
        router = DeterministicRouter([
            RulePack(agent="legal", subject_terms=("nda",)),
        ])
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="1",
            conversation_id="c", received_at=None, sender_email="x@example.com",
            sender_name="X", subject="Monday agenda", body_preview="See you then",
        )
        self.assertIsNone(router.route(msg).draft_owner)
        self.assertEqual(router.route(msg).outcome, "UNMATCHED")
        self.assertEqual(router.route(msg).disposition.value, "review_required")

    def test_unknown_sender_with_important_request_is_retained_for_review(self):
        router = DeterministicRouter([
            RulePack(agent="primary", subject_terms=("redwood",)),
        ])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="new-lender",
            conversation_id="new-lender", received_at=None,
            sender_email="new.lender@example.com", sender_name="New Lender",
            subject="Approval needed by 3 PM",
            body_preview="Please confirm whether you approve the revised terms.",
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "UNMATCHED")
        self.assertEqual(decision.disposition.value, "review_required")
        self.assertEqual(decision.reasons, ("NO_ROUTING_SIGNAL",))

    def test_human_out_of_office_coverage_request_is_not_hard_dropped(self):
        router = DeterministicRouter([
            RulePack(agent="primary", subject_terms=("project redwood",)),
        ])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="coverage",
            conversation_id="coverage", received_at=None,
            sender_email="person@example.com", sender_name="Person",
            subject="Out of office coverage for Project Redwood",
            body_preview="Can you cover the lender call tomorrow?",
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "ROUTED")
        self.assertEqual(decision.draft_owner, "primary")

    def test_no_reply_sender_is_dropped_even_when_subject_matches_deal(self):
        router = DeterministicRouter([
            RulePack(agent="primary", subject_terms=("project redwood",)),
        ])
        message = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="automated",
            conversation_id="automated", received_at=None,
            sender_email="no-reply@example.com", sender_name="Notifications",
            subject="Project Redwood status update", body_preview="Automated status.",
        )
        decision = router.route(message)
        self.assertEqual(decision.outcome, "DROPPED")
        self.assertIn("global-noise:automated-sender:no-reply@", decision.reasons)

    def test_quoted_history_does_not_route_or_escalate(self):
        router = DeterministicRouter([
            RulePack(
                agent="legal", body_terms=("redline",),
                critical_terms=("redline",), threshold=30,
            ),
        ])
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="1",
            conversation_id="c", received_at=None, sender_email="x@example.com",
            sender_name="X", subject="Lunch", body_preview=None,
            body_content="Sounds good.<blockquote>Prior redline discussion</blockquote>",
        )
        decision = router.route(msg)
        self.assertIsNone(decision.draft_owner)
        self.assertEqual(decision.priority, Priority.P3)

    def test_nested_terms_count_as_one_subject_signal(self):
        router = DeterministicRouter([
            RulePack(agent="primary", subject_terms=("redwood", "project redwood")),
        ])
        msg = IncomingMessage(
            mailbox="operator@example.com", provider_message_id="1",
            conversation_id="c", received_at=None, sender_email="x@example.com",
            sender_name="X", subject="Project Redwood", body_preview="Hello",
        )
        decision = router.route(msg)
        self.assertEqual(decision.reasons, ("subject:project redwood",))
        self.assertEqual(decision.confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
