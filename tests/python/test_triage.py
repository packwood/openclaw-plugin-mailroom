from __future__ import annotations

import json
import unittest
import urllib.error

from mailroom.models import (
    Disposition,
    IncomingMessage,
    Priority,
    RouteDecision,
    TriageLevel,
)
from mailroom.responsibility_profiles import (
    AgentResponsibilityProfile,
    FleetProfileSet,
    GenerationMetadata,
)
from mailroom.triage import (
    GeminiFlashLiteTriager,
    OwnerCandidate,
    SemanticTriage,
    SemanticTriageAvailabilityError,
    SemanticTriageError,
    UnsafeSemanticOutputError,
    apply_semantic_triage,
)


def profile(agent_id: str, specific: str, shared: str = "financial modeling"):
    return AgentResponsibilityProfile.from_dict(
        {
            "agent_id": agent_id,
            "mission": f"Own {specific}",
            "distinctive_specialties": [specific],
            "domains": [],
            "industries": [],
            "project_transaction_types": [],
            "functional_responsibilities": [],
            "named_entities": {
                "companies": [],
                "projects": [specific],
                "counterparties": [],
                "people": [],
            },
            "differentiating_signals": [specific],
            "shared_capabilities": [shared],
            "positive_routing_signals": [specific],
            "negative_routing_signals": [],
            "ambiguity_guidance": [],
        },
        routing_only=True,
    )


def profile_set() -> FleetProfileSet:
    generation = GenerationMetadata(
        model="anthropic/claude-sonnet-5",
        generator_agent="main",
        prompt_version="v1",
        corpus_provider="test",
        run_id="run",
        started_at="start",
        completed_at="end",
        fleet_refinement=True,
    )
    return FleetProfileSet.build(
        [
            profile("primary", "Project Redwood"),
            profile("research", "Example Research"),
        ],
        generation,
        generated_at="2026-07-17T00:00:00Z",
    )


def message() -> IncomingMessage:
    return IncomingMessage(
        mailbox="operator@example.com",
        provider_message_id="message",
        conversation_id="conversation",
        received_at="2026-07-16T14:00:00Z",
        sender_email="lender@example.com",
        sender_name="Lender",
        subject="Project Redwood approval needed today",
        body_preview="Please approve the revised terms by 4 PM.",
        has_attachments=True,
        raw={
            "toRecipients": [{"emailAddress": {"address": "operator@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "team@example.com"}}],
        },
    )


def response(**overrides):
    value = {
        "action": "reply",
        "candidates": [
            {
                "owner": "primary",
                "confidence": 0.96,
                "reasons": ["The email concerns Project Redwood."],
                "specific_signals": ["Project Redwood"],
                "shared_signals": [],
            },
            {
                "owner": "research",
                "confidence": 0.20,
                "reasons": ["No maritime evidence is present."],
                "specific_signals": [],
                "shared_signals": [],
            },
        ],
        "importance": "high",
        "urgency": "high",
        "classification_confidence": 0.96,
        "rationale": "The sender asks for approval by 4 PM.",
    }
    value.update(overrides)
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}]}


class GeminiTriagerTests(unittest.TestCase):
    def test_calls_flash_lite_with_complete_profiles_and_ranked_schema(self):
        calls = []

        def fetch(url, headers, payload, timeout):
            calls.append((url, headers, payload, timeout))
            return response()

        profiles = profile_set()
        result = GeminiFlashLiteTriager(
            "secret",
            profiles,
            fetch_json=fetch,
        ).triage(
            message(),
            RouteDecision(
                draft_owner="primary",
                watchers=(),
                confidence=0.8,
                reasons=("subject:redwood",),
                outcome="ROUTED",
            ),
        )
        self.assertEqual(result.candidates[0].owner, "primary")
        url, headers, payload, timeout = calls[0]
        self.assertTrue(url.endswith("/gemini-3.1-flash-lite:generateContent"))
        self.assertNotIn("secret", url)
        self.assertEqual(headers["x-goog-api-key"], "secret")
        schema = payload["generationConfig"]["responseJsonSchema"]
        owner_enum = schema["properties"]["candidates"]["items"]["properties"]["owner"][
            "enum"
        ]
        self.assertEqual(owner_enum, ["primary", "research"])
        prompt = payload["contents"][0]["parts"][0]["text"]
        self.assertIn(profiles.profile_set_id, prompt)
        self.assertIn("Example Research", prompt)
        self.assertNotIn("Owner scopes", prompt)
        self.assertIn("team@example.com", prompt)
        self.assertEqual(timeout, 20.0)

    def test_retries_transient_http_error_once(self):
        calls = []

        def fetch(*_args):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    "https://example.com", 429, "rate", {}, None
                )
            return response()

        result = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=fetch,
        ).triage(message(), RouteDecision(None, (), 0, ()))
        self.assertEqual(result.action, "reply")
        self.assertEqual(len(calls), 2)

    def test_retries_validation_error_with_bounded_corrective_turn(self):
        calls = []
        invalid_candidates = json.loads(
            response()["candidates"][0]["content"]["parts"][0]["text"]
        )["candidates"]
        invalid_candidates[0]["specific_signals"] = ["financial modeling"]

        def fetch(_url, _headers, payload, _timeout):
            calls.append(payload)
            return response(candidates=invalid_candidates) if len(calls) == 1 else response()

        result = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=fetch,
        ).triage(message(), RouteDecision(None, (), 0, ()))

        self.assertEqual(result.candidates[0].owner, "primary")
        self.assertEqual(len(calls), 2)
        repair = calls[1]["contents"][-1]["parts"][0]["text"]
        self.assertIn("mislabels a shared signal as specific", repair)
        self.assertIn("Remove an ungrounded candidate", repair)
        self.assertIn('"shared_signals":["financial modeling"]', repair)
        self.assertIn('"specific_signals":["project redwood"]', repair.lower())

    def test_provider_failure_is_classified_as_availability_error(self):
        triager = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=lambda *_: (_ for _ in ()).throw(
                urllib.error.HTTPError("https://example.com", 503, "down", {}, None)
            ),
        )
        with self.assertRaisesRegex(SemanticTriageAvailabilityError, "HTTP 503"):
            triager.triage(message(), RouteDecision(None, (), 0, ()))

    def test_deterministically_repairs_exact_misplaced_evidence_after_retry(self):
        invalid_candidates = json.loads(
            response()["candidates"][0]["content"]["parts"][0]["text"]
        )["candidates"]
        invalid_candidates[0]["specific_signals"] = ["financial modeling"]
        invalid_candidates[0]["shared_signals"] = ["Project Redwood"]
        result = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=lambda *_: response(candidates=invalid_candidates),
        ).triage(message(), RouteDecision(None, (), 0, ()))

        self.assertTrue(result.evidence_repaired)
        self.assertEqual(
            result.candidates[0].specific_signals, ("project redwood",)
        )
        self.assertEqual(
            result.candidates[0].shared_signals, ("financial modeling",)
        )

    def test_unknown_owner_is_unsafe_output(self):
        value = response()["candidates"][0]["content"]["parts"][0]["text"]
        decoded = json.loads(value)
        decoded["candidates"][0]["owner"] = "support"
        triager = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=lambda *_: response(candidates=decoded["candidates"]),
        )
        with self.assertRaises(UnsafeSemanticOutputError):
            triager.triage(message(), RouteDecision(None, (), 0, ()))

    def test_rejects_unranked_candidates(self):
        candidates = response()["candidates"][0]["content"]["parts"][0]["text"]
        candidates = json.loads(candidates)["candidates"]
        candidates[0]["confidence"], candidates[1]["confidence"] = 0.4, 0.6
        triager = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=lambda *_: response(candidates=candidates),
        )
        with self.assertRaisesRegex(SemanticTriageError, "not ranked"):
            triager.triage(message(), RouteDecision(None, (), 0, ()))

    def test_shared_term_claimed_as_specific_is_repaired_not_trusted(self):
        candidates = response()["candidates"][0]["content"]["parts"][0]["text"]
        candidates = json.loads(candidates)["candidates"]
        candidates[0]["specific_signals"] = ["financial modeling"]
        triager = GeminiFlashLiteTriager(
            "secret",
            profile_set(),
            fetch_json=lambda *_: response(candidates=candidates),
        )
        result = triager.triage(message(), RouteDecision(None, (), 0, ()))
        self.assertTrue(result.evidence_repaired)
        self.assertNotIn(
            "financial modeling", result.candidates[0].specific_signals
        )
        self.assertIn("financial modeling", result.candidates[0].shared_signals)


class TriagePolicyTests(unittest.TestCase):
    def semantic(self, **overrides):
        value = {
            "action": "reply",
            "candidates": (
                OwnerCandidate(
                    owner="primary",
                    confidence=0.95,
                    reasons=("Project match",),
                    specific_signals=("Project Redwood",),
                    shared_signals=(),
                ),
                OwnerCandidate(
                    owner="research",
                    confidence=0.20,
                    reasons=("Weak alternative",),
                    specific_signals=(),
                    shared_signals=(),
                ),
            ),
            "importance": TriageLevel.MEDIUM,
            "urgency": TriageLevel.MEDIUM,
            "classification_confidence": 0.95,
            "rationale": "A response is requested.",
            "model": "gemini-3.1-flash-lite",
            "profile_set_id": "arp-test",
        }
        value.update(overrides)
        return SemanticTriage(**value)

    def test_high_confidence_low_low_no_reply_is_safely_dropped(self):
        decision = apply_semantic_triage(
            RouteDecision("primary", (), 0.8, ("subject",), outcome="ROUTED"),
            self.semantic(
                action="no_reply",
                candidates=(),
                importance=TriageLevel.LOW,
                urgency=TriageLevel.LOW,
                classification_confidence=0.91,
            ),
        )
        self.assertEqual(decision.outcome, "DROPPED")
        self.assertEqual(decision.disposition, Disposition.FYI)
        self.assertEqual(decision.priority, Priority.P3)

    def test_strong_separated_specific_candidate_routes(self):
        decision = apply_semantic_triage(
            RouteDecision(None, (), 0, ("NO_ROUTING_SIGNAL",), outcome="UNMATCHED"),
            self.semantic(importance=TriageLevel.HIGH, urgency=TriageLevel.CRITICAL),
        )
        self.assertEqual((decision.outcome, decision.draft_owner), ("ROUTED", "primary"))
        self.assertEqual(decision.priority, Priority.P0)
        self.assertEqual(decision.watchers, ("research",))

    def test_close_runner_up_goes_to_review(self):
        candidates = (
            OwnerCandidate("primary", 0.91, ("Deal",), ("Project Redwood",), ()),
            OwnerCandidate(
                "research", 0.83, ("Also plausible",), ("Example Research",), ()
            ),
        )
        decision = apply_semantic_triage(
            RouteDecision("primary", (), 0.9, ("subject:redwood",), outcome="ROUTED"),
            self.semantic(candidates=candidates),
            min_owner_separation=0.15,
        )
        self.assertEqual(decision.outcome, "BORDERLINE")
        self.assertIsNone(decision.draft_owner)

    def test_shared_only_candidate_goes_to_review_even_when_confident(self):
        candidate = OwnerCandidate(
            "primary",
            0.99,
            ("Generic capability",),
            (),
            ("financial modeling",),
        )
        decision = apply_semantic_triage(
            RouteDecision("primary", (), 0.9, ("subject:model",), outcome="ROUTED"),
            self.semantic(candidates=(candidate,)),
        )
        self.assertEqual(decision.outcome, "BORDERLINE")

    def test_low_confidence_top_candidate_goes_to_review(self):
        candidate = OwnerCandidate(
            "primary",
            0.84,
            ("Deal",),
            ("Project Redwood",),
            (),
        )
        decision = apply_semantic_triage(
            RouteDecision(None, (), 0, ()),
            self.semantic(candidates=(candidate,)),
        )
        self.assertEqual(decision.outcome, "BORDERLINE")

    def test_deterministic_owner_does_not_bypass_candidate_policy(self):
        candidates = (
            OwnerCandidate("primary", 0.60, ("Weak",), ("Project Redwood",), ()),
            OwnerCandidate("research", 0.55, ("Close",), ("Example Research",), ()),
        )
        decision = apply_semantic_triage(
            RouteDecision("primary", (), 0.99, ("subject:redwood",), outcome="ROUTED"),
            self.semantic(candidates=candidates),
        )
        self.assertEqual(decision.outcome, "BORDERLINE")


if __name__ == "__main__":
    unittest.main()
