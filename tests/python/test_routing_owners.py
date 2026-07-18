from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailroom.routing_owners import RoutingOwnerPolicy, RoutingOwnerPolicyError


class RoutingOwnerPolicyTests(unittest.TestCase):
    def test_missing_policy_defaults_to_all_available_agents(self):
        with tempfile.TemporaryDirectory() as td:
            policy = RoutingOwnerPolicy.load(Path(td) / "missing.json")
        self.assertEqual(policy.resolve(["research", "primary", "research"]), (
            "primary", "research"
        ))

    def test_selected_policy_filters_available_agents(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "mode": "selected",
                "selected_agent_ids": ["research"],
            }), encoding="utf-8")
            policy = RoutingOwnerPolicy.load(path)
        self.assertEqual(policy.resolve(["primary", "research"]), ("research",))

    def test_selected_policy_rejects_agents_missing_from_the_fleet(self):
        policy = RoutingOwnerPolicy(
            mode="selected", selected_agent_ids=("missing",)
        )
        with self.assertRaisesRegex(RoutingOwnerPolicyError, "not available"):
            policy.resolve(["primary"])

    def test_invalid_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.json"
            path.write_text('{"schema_version": 9}', encoding="utf-8")
            with self.assertRaisesRegex(RoutingOwnerPolicyError, "invalid"):
                RoutingOwnerPolicy.load(path)


if __name__ == "__main__":
    unittest.main()
