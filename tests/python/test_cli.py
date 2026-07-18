from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mailroom.cli import _edit_json_in_editor, _triage_cycle_failed, main
from mailroom.controller import ShadowRunSummary
from mailroom.profile_generation import (
    FleetDiscoveryError,
    OpenClawFleetDiscovery,
    ProfileGenerationError,
)
from mailroom.responsibility_profiles import (
    AgentResponsibilityProfile,
    FleetProfileSet,
    GenerationMetadata,
    ProfileOverrideStore,
    ProfileStore,
    ProfileStoreError,
)


def profiles():
    profile = AgentResponsibilityProfile.from_dict(
        {
            "agent_id": "primary",
            "mission": "Own Redwood",
            "distinctive_specialties": ["Project Redwood"],
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
            "differentiating_signals": ["Project Redwood"],
            "shared_capabilities": ["financial modeling"],
            "positive_routing_signals": ["Project Redwood"],
            "negative_routing_signals": [],
            "ambiguity_guidance": [],
        },
        routing_only=True,
    )
    metadata = GenerationMetadata(
        model="sonnet",
        generator_agent="main",
        prompt_version="v1",
        corpus_provider="test",
        run_id="run",
        started_at="start",
        completed_at="end",
        fleet_refinement=True,
    )
    return FleetProfileSet.build([profile], metadata)


class FakeProfileStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def load_current(self):
        return profiles()


class MissingProfileStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def load_current(self):
        raise ProfileStoreError("missing")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.mailroom_secret_patch = mock.patch(
            "mailroom.cli.mailroom_secret_value",
            return_value="mailroom-provider-key",
        )
        self.mailroom_secret_patch.start()
        self.addCleanup(self.mailroom_secret_patch.stop)

    def test_profile_generation_failure_returns_sanitized_structured_error(self):
        class FailingGenerator:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self):
                try:
                    raise RuntimeError("sensitive provider diagnostic")
                except RuntimeError as exc:
                    raise ProfileGenerationError("sanitized failure") from exc

        printed = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.ResponsibilityProfileGenerator",
                FailingGenerator,
            ),
            mock.patch("mailroom.cli._print", side_effect=printed.append),
        ):
            result = main(
                [
                    "profiles",
                    "generate",
                    "--profiles-dir",
                    td,
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            printed,
            [
                {
                    "ok": False,
                    "error": "profile_generation_failed",
                    "message": "sanitized failure",
                }
            ],
        )
        self.assertNotIn("sensitive", repr(printed))

    def test_fleet_discovery_failure_returns_sanitized_structured_error(self):
        class FailingDiscovery:
            def discover(self):
                raise FleetDiscoveryError(
                    "OpenClaw fleet discovery returned invalid JSON"
                )

        printed = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.OpenClawFleetDiscovery",
                return_value=FailingDiscovery(),
            ),
            mock.patch("mailroom.cli._print", side_effect=printed.append),
        ):
            result = main(
                [
                    "profiles",
                    "generate",
                    "--profiles-dir",
                    td,
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            printed,
            [
                {
                    "ok": False,
                    "error": "fleet_discovery_failed",
                    "message": "OpenClaw fleet discovery returned invalid JSON",
                }
            ],
        )

    def test_profile_edit_publishes_manual_override(self):
        printed = []
        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / "profiles"
            overrides_dir = Path(td) / "overrides"
            store = ProfileStore(profiles_dir)
            base = store.publish(profiles())

            with mock.patch("mailroom.cli._print", side_effect=printed.append):
                result = main(
                    [
                        "profiles",
                        "edit",
                        "primary",
                        "--profiles-dir",
                        str(profiles_dir),
                        "--overrides-dir",
                        str(overrides_dir),
                        "--set-json",
                        '{"mission":"Own Project Redwood and LP follow-up"}',
                    ]
                )

            current = store.load_current()

        self.assertEqual(result, 0)
        self.assertTrue(printed[0]["published"])
        self.assertTrue(printed[0]["changed"])
        self.assertNotEqual(printed[0]["profile_set_id"], base.profile_set_id)
        self.assertEqual(
            current.profile("primary").mission,
            "Own Project Redwood and LP follow-up",
        )

    def test_profile_clear_restores_generated_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / "profiles"
            overrides_dir = Path(td) / "overrides"
            store = ProfileStore(profiles_dir)
            store.publish(profiles())
            with mock.patch("mailroom.cli._print"):
                self.assertEqual(
                    main(
                        [
                            "profiles", "edit", "primary",
                            "--profiles-dir", str(profiles_dir),
                            "--overrides-dir", str(overrides_dir),
                            "--set-json", '{"mission":"Temporary override"}',
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "profiles", "edit", "primary", "--clear",
                            "--profiles-dir", str(profiles_dir),
                            "--overrides-dir", str(overrides_dir),
                        ]
                    ),
                    0,
                )

            self.assertEqual(store.load_current().profile("primary").mission, "Own Redwood")
            self.assertEqual(
                store.load_generated_baseline().profile("primary").mission,
                "Own Redwood",
            )

    def test_profile_edit_rolls_back_override_when_publication_fails(self):
        printed = []
        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / "profiles"
            overrides_dir = Path(td) / "overrides"
            store = ProfileStore(profiles_dir)
            store.publish_generated(profiles(), profiles())
            overrides = ProfileOverrideStore(overrides_dir)
            overrides.save("retired", {"agent_id": "retired", "mission": "Stale"})

            with mock.patch("mailroom.cli._print", side_effect=printed.append):
                result = main(
                    [
                        "profiles", "edit", "primary",
                        "--profiles-dir", str(profiles_dir),
                        "--overrides-dir", str(overrides_dir),
                        "--set-json", '{"mission":"Must roll back"}',
                    ]
                )

            self.assertEqual(result, 2)
            self.assertEqual(printed[0]["error"], "profile_edit_failed")
            self.assertFalse(overrides.path_for("primary").exists())

    def test_profile_edit_unknown_agent_returns_structured_error(self):
        printed = []
        with tempfile.TemporaryDirectory() as td:
            store = ProfileStore(Path(td) / "profiles")
            store.publish(profiles())
            with mock.patch("mailroom.cli._print", side_effect=printed.append):
                result = main(
                    [
                        "profiles", "edit", "missing-agent", "--clear",
                        "--profiles-dir", str(store.root),
                        "--overrides-dir", str(Path(td) / "overrides"),
                    ]
                )
        self.assertEqual(result, 2)
        self.assertEqual(printed[0]["error"], "unknown_profile_agent")

    def test_editor_command_supports_arguments(self):
        with (
            mock.patch.dict("os.environ", {"EDITOR": "code --wait", "VISUAL": ""}),
            mock.patch("mailroom.cli.subprocess.run") as run,
        ):
            edited = _edit_json_in_editor(
                {"agent_id": "primary", "mission": "Own Redwood"},
                agent_id="primary",
            )
        self.assertEqual(edited["mission"], "Own Redwood")
        self.assertEqual(run.call_args.args[0][:2], ["code", "--wait"])

    def test_profile_validate_reports_override_status(self):
        printed = []
        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / "profiles"
            overrides_dir = Path(td) / "overrides"
            ProfileStore(profiles_dir).publish(profiles())
            with mock.patch("mailroom.cli._print"):
                main(
                    [
                        "profiles",
                        "edit",
                        "primary",
                        "--profiles-dir",
                        str(profiles_dir),
                        "--overrides-dir",
                        str(overrides_dir),
                        "--set-json",
                        '{"domains":["investor relations"]}',
                        "--no-publish",
                    ]
                )
            with mock.patch("mailroom.cli._print", side_effect=printed.append):
                result = main(
                    [
                        "profiles",
                        "validate",
                        "--profiles-dir",
                        str(profiles_dir),
                        "--overrides-dir",
                        str(overrides_dir),
                    ]
                )

        self.assertEqual(result, 0)
        self.assertTrue(printed[0]["ok"])
        self.assertTrue(printed[0]["changed_by_overrides"])
        self.assertEqual(printed[0]["override_agent_ids"], ("primary",))

    def test_profile_validate_reports_malformed_override_without_traceback(self):
        printed = []
        with tempfile.TemporaryDirectory() as td:
            profiles_dir = Path(td) / "profiles"
            overrides_dir = Path(td) / "overrides"
            ProfileStore(profiles_dir).publish(profiles())
            overrides_dir.mkdir()
            (overrides_dir / "primary.json").write_text("{bad json", encoding="utf-8")
            with mock.patch("mailroom.cli._print", side_effect=printed.append):
                result = main(
                    [
                        "profiles",
                        "validate",
                        "--profiles-dir",
                        str(profiles_dir),
                        "--overrides-dir",
                        str(overrides_dir),
                    ]
                )

        self.assertEqual(result, 2)
        self.assertEqual(printed[0]["ok"], False)
        self.assertEqual(printed[0]["error"], "override_validation_failed")
        self.assertIn("Profile override is unreadable", printed[0]["message"])

    def test_profile_generate_reports_stale_override_as_structured_failure(self):
        class StaleOverrideGenerator:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self):
                raise ProfileGenerationError(
                    "Profile override validation failed: profile override references unknown agent(s): retired"
                )

        printed = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.ResponsibilityProfileGenerator",
                StaleOverrideGenerator,
            ),
            mock.patch("mailroom.cli._print", side_effect=printed.append),
        ):
            result = main(
                [
                    "profiles",
                    "generate",
                    "--profiles-dir",
                    td,
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            printed,
            [
                {
                    "ok": False,
                    "error": "profile_generation_failed",
                    "message": "Profile override validation failed: profile override references unknown agent(s): retired",
                }
            ],
        )

    def test_discovery_subprocess_failures_reach_structured_cli_boundary(self):
        cases = (
            (
                FileNotFoundError("sensitive missing path"),
                "OpenClaw fleet discovery executable is unavailable",
            ),
            (
                PermissionError("sensitive permission detail"),
                "OpenClaw fleet discovery executable is not permitted",
            ),
            (
                subprocess.TimeoutExpired(
                    ["sensitive-command"],
                    60,
                    output="private output",
                    stderr="secret error",
                ),
                "OpenClaw fleet discovery timed out after 60 seconds",
            ),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected):

                def run(*_args, **_kwargs):
                    raise failure

                printed = []
                discovery = OpenClawFleetDiscovery(
                    run=run,
                    timeout_seconds=60,
                )
                with (
                    tempfile.TemporaryDirectory() as td,
                    mock.patch(
                        "mailroom.cli.OpenClawFleetDiscovery",
                        return_value=discovery,
                    ),
                    mock.patch(
                        "mailroom.cli._print",
                        side_effect=printed.append,
                    ),
                ):
                    result = main(
                        [
                            "profiles",
                            "generate",
                            "--profiles-dir",
                            td,
                        ]
                    )

                self.assertEqual(result, 2)
                self.assertEqual(
                    printed,
                    [
                        {
                            "ok": False,
                            "error": "fleet_discovery_failed",
                            "message": expected,
                        }
                    ],
                )
                self.assertNotIn("sensitive", repr(printed))
                self.assertNotIn("private", repr(printed))
                self.assertNotIn("secret", repr(printed))

    def test_shadow_does_not_advance_intake_when_profiles_are_missing(self):
        class MustNotRunController:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("controller must not run")

        printed = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.env_value",
                return_value="configured",
            ),
            mock.patch(
                "mailroom.cli.resolve_connection",
                return_value="connection",
            ),
            mock.patch(
                "mailroom.cli.ProfileStore",
                MissingProfileStore,
            ),
            mock.patch(
                "mailroom.cli.ShadowController",
                MustNotRunController,
            ),
            mock.patch(
                "mailroom.cli._print",
                side_effect=printed.append,
            ),
        ):
            result = main(
                [
                    "--db",
                    str(Path(td) / "mailroom.db"),
                    "shadow",
                    "--account",
                    "operator@example.com",
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(printed[0]["profile_status"], "unavailable")

    def test_cycle_keeps_dispatch_and_reconciliation_alive_without_profiles(self):
        calls = []

        class FakeDispatcher:
            def __init__(self, *_args, **_kwargs):
                calls.append("dispatcher-init")

            def run(self):
                calls.append("dispatch-run")
                return SimpleNamespace(cards_sent=0)

        class FakeReconciler:
            def __init__(self, *_args, **_kwargs):
                calls.append("reconciler-init")

            def run(self):
                calls.append("reconcile-run")
                return SimpleNamespace(checked=0)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.env_value",
                return_value="configured",
            ),
            mock.patch(
                "mailroom.cli.resolve_connection",
                return_value="connection",
            ),
            mock.patch(
                "mailroom.cli.ProfileStore",
                MissingProfileStore,
            ),
            mock.patch(
                "mailroom.cli._review_owners",
                return_value=("primary",),
            ),
            mock.patch(
                "mailroom.cli.DraftDispatcher",
                FakeDispatcher,
            ),
            mock.patch(
                "mailroom.cli.SendReconciler",
                FakeReconciler,
            ),
            mock.patch(
                "mailroom.cli._print",
            ),
        ):
            result = main(
                [
                    "--db",
                    str(Path(td) / "mailroom.db"),
                    "cycle",
                    "--account",
                    "operator@example.com",
                    "--telegram-chat-id",
                    "chat",
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(
            calls,
            [
                "dispatcher-init",
                "dispatch-run",
                "reconciler-init",
                "reconcile-run",
            ],
        )

    def test_shadow_returns_nonzero_when_semantic_triage_unavailable(self):
        class FailingSummaryController:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self):
                return ShadowRunSummary(triage_errors=1)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "mailroom.cli.env_value",
                return_value="configured",
            ),
            mock.patch(
                "mailroom.cli.resolve_connection",
                return_value="connection",
            ),
            mock.patch(
                "mailroom.cli.ShadowController",
                FailingSummaryController,
            ),
            mock.patch(
                "mailroom.cli.ProfileStore",
                FakeProfileStore,
            ),
            mock.patch(
                "mailroom.cli._print",
            ),
        ):
            result = main(
                [
                    "--db",
                    str(Path(td) / "mailroom.db"),
                    "shadow",
                    "--account",
                    "operator@example.com",
                ]
            )
        self.assertEqual(result, 2)

    def test_single_validation_degradation_does_not_fail_cycle_monitoring(self):
        self.assertFalse(
            _triage_cycle_failed(ShadowRunSummary(triage_degraded=1).__dict__)
        )

    def test_sustained_batch_validation_degradation_fails_monitoring(self):
        self.assertTrue(
            _triage_cycle_failed(ShadowRunSummary(triage_degraded=3).__dict__)
        )

    def test_successful_majority_prevents_batch_degradation_escalation(self):
        self.assertFalse(
            _triage_cycle_failed(
                ShadowRunSummary(triaged=4, triage_degraded=3).__dict__
            )
        )

    def test_missing_mailroom_gemini_key_ignores_global_keys_and_reports_degradation(self):
        captured = []

        class SuccessfulSummaryController:
            def __init__(self, *_args, **kwargs):
                captured.append(kwargs.get("triager"))

            def run(self):
                return ShadowRunSummary()

        def env_value(name):
            return "maton-key" if name == "MATON_API_KEY" else None

        printed = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.dict(
                "os.environ",
                {
                    "GEMINI_API_KEY": "global-gemini",
                    "GOOGLE_API_KEY": "global-google",
                },
                clear=False,
            ),
            mock.patch(
                "mailroom.cli.env_value",
                side_effect=env_value,
            ),
            mock.patch(
                "mailroom.cli.mailroom_secret_value",
                return_value=None,
            ),
            mock.patch(
                "mailroom.cli.resolve_connection",
                return_value="connection",
            ),
            mock.patch(
                "mailroom.cli.ShadowController",
                SuccessfulSummaryController,
            ),
            mock.patch(
                "mailroom.cli.ProfileStore",
                FakeProfileStore,
            ),
            mock.patch(
                "mailroom.cli._print",
                side_effect=printed.append,
            ),
        ):
            result = main(
                [
                    "--db",
                    str(Path(td) / "mailroom.db"),
                    "shadow",
                    "--account",
                    "operator@example.com",
                ]
            )

        self.assertEqual(result, 2)
        self.assertEqual(printed[0]["triage_errors"], 1)
        self.assertEqual(captured[0].api_key, "")


if __name__ == "__main__":
    unittest.main()
