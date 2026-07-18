from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailroom.responsibility_profiles import (
    AgentResponsibilityProfile,
    FleetProfileSet,
    GenerationMetadata,
    ProfileOverrideStore,
    ProfileStore,
    ProfileValidationError,
    diff_profile_sets,
)


def generation(run_id="run"):
    return GenerationMetadata(
        model="anthropic/claude-sonnet-5",
        generator_agent="main",
        prompt_version="v1",
        corpus_provider="test-provider",
        run_id=run_id,
        started_at="start",
        completed_at="end",
        fleet_refinement=True,
    )


def profile(agent_id="primary", mission="Own Project Redwood", shared=None):
    return AgentResponsibilityProfile.from_dict(
        {
            "agent_id": agent_id,
            "mission": mission,
            "distinctive_specialties": [mission],
            "domains": [],
            "industries": [],
            "project_transaction_types": [],
            "functional_responsibilities": [],
            "named_entities": {
                "companies": [],
                "projects": [],
                "counterparties": [],
                "people": [],
            },
            "differentiating_signals": [mission],
            "shared_capabilities": shared or ["financial modeling"],
            "positive_routing_signals": [mission],
            "negative_routing_signals": [],
            "ambiguity_guidance": [],
        },
        routing_only=True,
    )


class SchemaTests(unittest.TestCase):
    def test_rejects_routing_irrelevant_or_unknown_fields(self):
        value = profile().to_dict(routing_only=True)
        value["preferred_tools"] = ["Python"]
        with self.assertRaisesRegex(ProfileValidationError, "extra=preferred_tools"):
            AgentResponsibilityProfile.from_dict(value, routing_only=True)

    def test_complete_profile_set_is_sorted_and_content_addressed(self):
        first = FleetProfileSet.build(
            [profile("research", "Own maritime"), profile("primary")],
            generation("one"),
            generated_at="first",
        )
        second = FleetProfileSet.build(
            [profile("primary"), profile("research", "Own maritime")],
            generation("two"),
            generated_at="second",
        )
        self.assertEqual(first.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(first.profile_set_id, second.profile_set_id)
        self.assertEqual(FleetProfileSet.from_dict(first.to_dict()), first)

    def test_tampered_profile_id_is_rejected(self):
        value = FleetProfileSet.build([profile()], generation()).to_dict()
        value["profile_set_id"] = "arp-wrong"
        with self.assertRaisesRegex(ProfileValidationError, "does not match"):
            FleetProfileSet.from_dict(value)

    def test_duplicate_fleet_ids_are_rejected_even_if_profiles_are_unique(self):
        value = FleetProfileSet.build([profile()], generation()).to_dict()
        value["fleet_agent_ids"] = ["primary", "primary"]
        with self.assertRaisesRegex(ProfileValidationError, "duplicates"):
            FleetProfileSet.from_dict(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.temp.name) / "profiles")

    def tearDown(self):
        self.temp.cleanup()

    def test_publish_is_idempotent_and_creates_reviewable_diff(self):
        first = FleetProfileSet.build(
            [profile()], generation("one"), generated_at="one"
        )
        initial = self.store.publish(first)
        repeated = self.store.publish(
            FleetProfileSet.build(
                [profile()],
                generation("two"),
                generated_at="two",
            )
        )
        self.assertTrue(initial.changed)
        self.assertFalse(repeated.changed)
        self.assertEqual(self.store.load_current().profile_set_id, first.profile_set_id)
        self.assertTrue(initial.version_path.exists())
        self.assertTrue(initial.diff_path.exists())
        self.assertEqual(initial.diff.added_agents, ("primary",))
        self.assertEqual(
            (self.store.root / ".publish.lock").stat().st_mode & 0o777, 0o600
        )

    def test_current_is_last_commit_point_and_survives_publish_failure(self):
        first = FleetProfileSet.build(
            [profile()], generation("one"), generated_at="one"
        )
        self.store.publish(first)
        second = FleetProfileSet.build(
            [
                profile(),
                profile("research", "Own maritime"),
            ],
            generation("two"),
            generated_at="two",
        )
        from mailroom import responsibility_profiles as module

        original = module._atomic_write

        def fail_current(path, content):
            if path == self.store.current_path:
                raise OSError("simulated current write failure")
            return original(path, content)

        with patch.object(module, "_atomic_write", side_effect=fail_current):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.store.publish(second)
        self.assertEqual(self.store.load_current().profile_set_id, first.profile_set_id)

    def test_diff_reports_fleet_additions_removals_and_changes(self):
        before = FleetProfileSet.build(
            [
                profile("primary"),
                profile("support", "Own Redwood"),
            ],
            generation(),
            generated_at="one",
        )
        after = FleetProfileSet.build(
            [
                profile("primary", "Own changed Redwood"),
                profile("research", "Own maritime"),
            ],
            generation(),
            generated_at="two",
        )
        diff = diff_profile_sets(before, after)
        self.assertEqual(diff.added_agents, ("research",))
        self.assertEqual(diff.removed_agents, ("support",))
        self.assertEqual(diff.changed_agents, ("primary",))


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.overrides = ProfileOverrideStore(Path(self.temp.name) / "overrides")
        self.profile_set = FleetProfileSet.build(
            [profile("primary"), profile("support", "Own Redwood")],
            generation(),
            generated_at="base",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_override_merges_into_effective_profile_set(self):
        saved = self.overrides.save(
            "primary",
            {
                "agent_id": "primary",
                "mission": "Own Project Redwood and partner diligence",
                "named_entities": {"projects": ["Project Redwood"]},
            },
        )
        effective = self.overrides.apply(self.profile_set, generated_at="override")
        primary = effective.profile("primary")

        self.assertEqual(saved["mission"], "Own Project Redwood and partner diligence")
        self.assertEqual(primary.mission, "Own Project Redwood and partner diligence")
        self.assertEqual(primary.named_entities.projects, ("Project Redwood",))
        self.assertEqual(effective.generated_at, "override")
        self.assertNotEqual(effective.profile_set_id, self.profile_set.profile_set_id)

    def test_rejects_unknown_override_fields(self):
        with self.assertRaisesRegex(ProfileValidationError, "extra=preferred_tools"):
            self.overrides.save(
                "primary",
                {"agent_id": "primary", "preferred_tools": ["Python"]},
            )

    def test_rejects_overrides_for_agents_outside_the_fleet(self):
        self.overrides.save("unknown", {"agent_id": "unknown", "mission": "Own X"})
        with self.assertRaisesRegex(ProfileValidationError, "unknown agent"):
            self.overrides.apply(self.profile_set)


if __name__ == "__main__":
    unittest.main()
