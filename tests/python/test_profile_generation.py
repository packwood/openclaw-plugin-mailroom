from __future__ import annotations

import json
import subprocess
import tempfile
import traceback
import unittest
import urllib.error
from pathlib import Path

from mailroom.profile_generation import (
    AgentCorpus,
    AnthropicSonnetProfileModel,
    CorpusDocument,
    DirectWorkspaceCorpusProvider,
    FleetAgent,
    FleetDiscoveryError,
    OpenClawFleetDiscovery,
    ProfileGenerationError,
    ResponsibilityProfileGenerator,
)
from mailroom.responsibility_profiles import (
    ProfileOverrideStore,
    ProfileStore,
    SourceProvenance,
)


def raw_profile(agent_id, shared=None):
    responsibility = {
        "primary": "Project Redwood",
        "research": "Example Research",
        "support": "Redwood",
    }.get(agent_id, f"{agent_id} responsibilities")
    return {
        "agent_id": agent_id,
        "mission": f"Own {responsibility}",
        "distinctive_specialties": [responsibility],
        "domains": [],
        "industries": [],
        "project_transaction_types": [],
        "functional_responsibilities": [],
        "named_entities": {
            "companies": [],
            "projects": [responsibility],
            "counterparties": [],
            "people": [],
        },
        "differentiating_signals": [responsibility],
        "shared_capabilities": ["financial modeling"] if shared is None else shared,
        "positive_routing_signals": [responsibility],
        "negative_routing_signals": [],
        "ambiguity_guidance": [],
    }


class DiscoveryTests(unittest.TestCase):
    def test_live_fleet_additions_and_removals_follow_cli_output(self):
        outputs = [
            [{"id": "primary", "workspace": "/tmp/primary", "model": "sonnet"}],
            [
                {"id": "research", "workspace": "/tmp/research", "model": "sonnet"},
                {"id": "support", "workspace": "/tmp/support", "model": "sonnet"},
            ],
        ]

        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(outputs.pop(0)), stderr=""
            )

        discovery = OpenClawFleetDiscovery(run=run)
        self.assertEqual(
            tuple(agent.agent_id for agent in discovery.discover()), ("primary",)
        )
        self.assertEqual(
            tuple(agent.agent_id for agent in discovery.discover()), ("research", "support")
        )

    def test_invalid_or_duplicate_agent_records_are_rejected(self):
        def run(*_args, **_kwargs):
            values = [
                {"id": "primary", "workspace": "/tmp/one"},
                {"id": "primary", "workspace": "/tmp/two"},
            ]
            return subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(values), stderr=""
            )

        with self.assertRaisesRegex(Exception, "Duplicate agent"):
            OpenClawFleetDiscovery(run=run).discover()

    def test_command_invalid_json_and_empty_fleet_fail_with_safe_messages(self):
        cases = (
            (
                subprocess.CompletedProcess(
                    [], 7, stdout="private", stderr="secret"
                ),
                "exit code 7",
            ),
            (
                subprocess.CompletedProcess(
                    [], 0, stdout="not-json", stderr=""
                ),
                "invalid JSON",
            ),
            (
                subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
                "no agents",
            ),
        )
        for completed, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(FleetDiscoveryError) as caught:
                    OpenClawFleetDiscovery(
                        run=lambda *_args, **_kwargs: completed
                    ).discover()
                rendered = "".join(
                    traceback.format_exception(
                        type(caught.exception),
                        caught.exception,
                        caught.exception.__traceback__,
                    )
                )
                self.assertIn(expected, rendered)
                self.assertNotIn("private", rendered)
                self.assertNotIn("secret", rendered)

    def test_startup_and_timeout_failures_are_sanitized(self):
        cases = (
            (
                FileNotFoundError("sensitive missing path"),
                "executable is unavailable",
            ),
            (
                PermissionError("sensitive permission detail"),
                "executable is not permitted",
            ),
            (
                OSError("sensitive process detail"),
                "process could not start",
            ),
            (
                subprocess.TimeoutExpired(
                    ["sensitive-command"],
                    60,
                    output="private output",
                    stderr="secret error",
                ),
                "timed out after 60 seconds",
            ),
        )
        for failure, expected in cases:
            with self.subTest(expected=expected):

                def run(*_args, **_kwargs):
                    raise failure

                with self.assertRaises(FleetDiscoveryError) as caught:
                    OpenClawFleetDiscovery(
                        run=run,
                        timeout_seconds=60,
                    ).discover()
                rendered = "".join(
                    traceback.format_exception(
                        type(caught.exception),
                        caught.exception,
                        caught.exception.__traceback__,
                    )
                )
                self.assertIn(expected, rendered)
                for sensitive in (
                    "sensitive",
                    "private output",
                    "secret error",
                ):
                    self.assertNotIn(sensitive, rendered)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_core_memory_and_context_with_provenance(self):
        (self.workspace / "IDENTITY.md").write_text("Primary identity", encoding="utf-8")
        (self.workspace / "memory").mkdir()
        (self.workspace / "memory" / "deal.md").write_text(
            "Project Redwood", encoding="utf-8"
        )
        (self.workspace / "context").mkdir()
        (self.workspace / "context" / "terms.txt").write_text(
            "customer operations", encoding="utf-8"
        )
        corpus = DirectWorkspaceCorpusProvider().collect(
            FleetAgent("primary", self.workspace)
        )
        sources = {source.relative_path: source for source in corpus.sources}
        self.assertEqual(sources["IDENTITY.md"].category, "identity")
        self.assertEqual(sources["memory/deal.md"].status, "read")
        self.assertEqual(sources["context/terms.txt"].sha256.__len__(), 64)
        self.assertEqual(sources["AGENTS.md"].status, "missing")
        self.assertEqual(corpus.workspace_status, "available")

    def test_missing_workspace_reports_every_required_area(self):
        corpus = DirectWorkspaceCorpusProvider().collect(
            FleetAgent("primary", self.workspace / "absent"),
        )
        self.assertEqual(corpus.workspace_status, "missing")
        self.assertEqual(corpus.missing_count, 6)
        self.assertEqual(len(corpus.documents), 6)

    def test_unreadable_file_is_isolated_and_other_files_continue(self):
        blocked = self.workspace / "IDENTITY.md"
        good = self.workspace / "SOUL.md"
        blocked.write_text("secret", encoding="utf-8")
        good.write_text("good", encoding="utf-8")

        def read(path):
            if path == blocked:
                raise PermissionError("denied")
            return path.read_bytes()

        corpus = DirectWorkspaceCorpusProvider(read_bytes=read).collect(
            FleetAgent("primary", self.workspace),
        )
        sources = {source.relative_path: source for source in corpus.sources}
        self.assertEqual(sources["IDENTITY.md"].status, "unreadable")
        self.assertEqual(sources["SOUL.md"].status, "read")
        self.assertEqual(corpus.unreadable_count, 1)

    def test_per_file_and_agent_limits_report_truncation(self):
        (self.workspace / "IDENTITY.md").write_text("a" * 30, encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("b" * 30, encoding="utf-8")
        corpus = DirectWorkspaceCorpusProvider(
            max_file_chars=20,
            max_agent_chars=25,
        ).collect(FleetAgent("primary", self.workspace))
        sources = {source.relative_path: source for source in corpus.sources}
        self.assertTrue(corpus.truncated)
        self.assertEqual(corpus.total_included_chars, 25)
        self.assertEqual(sources["IDENTITY.md"].included_chars, 20)
        self.assertEqual(sources["AGENTS.md"].included_chars, 5)
        self.assertTrue(sources["AGENTS.md"].truncated)

    def test_file_count_limit_is_bounded_and_reported(self):
        (self.workspace / "memory").mkdir()
        for index in range(6):
            (self.workspace / "memory" / f"{index}.md").write_text(
                str(index), encoding="utf-8"
            )
        corpus = DirectWorkspaceCorpusProvider(max_files=4).collect(
            FleetAgent("primary", self.workspace),
        )
        omitted = [
            source
            for source in corpus.sources
            if "omitted by max_files" in source.relative_path
        ]
        self.assertEqual(len(omitted), 1)
        self.assertTrue(omitted[0].truncated)
        self.assertIn("2 additional files", omitted[0].detail)
        self.assertTrue(corpus.coverage_summary()["issues"])

    def test_context_is_not_starved_by_large_memory_corpus(self):
        (self.workspace / "context").mkdir()
        (self.workspace / "context" / "current.md").write_text(
            "c" * 100, encoding="utf-8"
        )
        (self.workspace / "memory").mkdir()
        (self.workspace / "memory" / "2026-07-17.md").write_text(
            "m" * 100, encoding="utf-8"
        )
        corpus = DirectWorkspaceCorpusProvider(
            max_file_chars=100,
            max_agent_chars=60,
            memory_reserve_chars=20,
        ).collect(FleetAgent("primary", self.workspace))
        sources = {source.relative_path: source for source in corpus.sources}
        self.assertEqual(sources["context/current.md"].included_chars, 40)
        self.assertEqual(sources["memory/2026-07-17.md"].included_chars, 20)

    def test_root_memory_and_newer_dates_precede_nested_archives(self):
        (self.workspace / "memory" / "dreaming").mkdir(parents=True)
        (self.workspace / "memory" / "2026-07-17.md").write_text(
            "newest", encoding="utf-8"
        )
        (self.workspace / "memory" / "dreaming" / "archive.md").write_text(
            "archive", encoding="utf-8"
        )
        corpus = DirectWorkspaceCorpusProvider(
            max_file_chars=20,
            max_agent_chars=6,
        ).collect(FleetAgent("primary", self.workspace))
        sources = {source.relative_path: source for source in corpus.sources}
        self.assertEqual(sources["memory/2026-07-17.md"].included_chars, 6)
        self.assertEqual(sources["memory/dreaming/archive.md"].included_chars, 0)


class ModelInvocationTests(unittest.TestCase):
    def test_uses_stateless_anthropic_messages_api_with_scoped_key(self):
        observed = {}

        def fetch(url, headers, payload, timeout):
            observed.update(
                url=url,
                headers=headers,
                payload=payload,
                timeout=timeout,
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": 'MAILROOM_PROFILE_JSON\n{"ok":true}',
                    }
                ]
            }

        model = AnthropicSonnetProfileModel(
            api_key="mailroom-key", fetch_json=fetch
        )
        result = model.generate(
            "private corpus prompt",
            session_key="mailroom-profile-run-primary",
            marker="MAILROOM_PROFILE_JSON",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(observed["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(observed["headers"]["x-api-key"], "mailroom-key")
        self.assertEqual(observed["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(observed["payload"]["model"], "claude-sonnet-5")
        self.assertEqual(observed["payload"]["max_tokens"], 32768)
        self.assertEqual(
            observed["payload"]["output_config"], {"effort": "medium"}
        )
        self.assertNotIn("temperature", observed["payload"])
        self.assertNotIn("thinking", observed["payload"])
        self.assertEqual(
            observed["payload"]["messages"],
            [{"role": "user", "content": "private corpus prompt"}],
        )

    def test_failure_does_not_echo_captured_sensitive_output(self):
        def fetch(url, *_args):
            raise urllib.error.HTTPError(
                url,
                401,
                "sensitive provider details",
                hdrs=None,
                fp=None,
            )

        model = AnthropicSonnetProfileModel(
            api_key="mailroom-key", fetch_json=fetch
        )
        with self.assertRaises(ProfileGenerationError) as caught:
            model.generate("prompt", session_key="session", marker="MARKER")
        rendered = "".join(
            traceback.format_exception(
                type(caught.exception),
                caught.exception,
                caught.exception.__traceback__,
            )
        )
        self.assertNotIn("sensitive", rendered)
        self.assertNotIn("secret", rendered)
        self.assertIn("Anthropic profile API returned HTTP 401", rendered)

    def test_reports_thinking_only_budget_exhaustion_without_response_content(self):
        def fetch(*_args):
            return {
                "content": [{"type": "thinking", "thinking": "private"}],
                "stop_reason": "max_tokens",
            }

        model = AnthropicSonnetProfileModel(
            api_key="mailroom-key", fetch_json=fetch
        )
        with self.assertRaisesRegex(ProfileGenerationError, "output budget"):
            model.generate("prompt", session_key="session", marker="MARKER")


class StaticDiscovery:
    def __init__(self, agents):
        self.agents = tuple(agents)

    def discover(self):
        return self.agents


class StaticProvider:
    provider_id = "fake-provider"

    def __init__(self):
        self.calls = []

    def collect(self, agent):
        self.calls.append(agent.agent_id)
        source = SourceProvenance(
            agent_id=agent.agent_id,
            relative_path="IDENTITY.md",
            category="identity",
            status="read",
            size_bytes=4,
            included_chars=4,
            sha256="0" * 64,
            truncated=False,
        )
        return AgentCorpus(
            agent=agent,
            documents=(CorpusDocument(source, "test"),),
            total_included_chars=4,
            truncated=False,
            missing_count=0,
            unreadable_count=0,
            workspace_status="available",
        )


class SelectivelyFailingProvider(StaticProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = set(failures)

    def collect(self, agent):
        if agent.agent_id in self.failures:
            raise PermissionError("sensitive underlying error")
        return super().collect(agent)


class FakeModel:
    model_id = "anthropic/claude-sonnet-5"
    generator_agent = "main"

    def __init__(self, *, fail=False, omit_refined=None):
        self.fail = fail
        self.omit_refined = omit_refined
        self.calls = []

    def generate(self, prompt, *, session_key, marker):
        self.calls.append((prompt, session_key, marker))
        if self.fail:
            raise ProfileGenerationError("model failed")
        if marker == "MAILROOM_PROFILE_JSON":
            agent_id = prompt.split("Agent ID (must be returned exactly): ", 1)[
                1
            ].splitlines()[0]
            return raw_profile(agent_id)
        ids = json.loads(prompt.split("Fleet IDs: ", 1)[1].splitlines()[0])
        return {
            "profiles": [
                raw_profile(agent_id, shared=[])
                for agent_id in ids
                if agent_id != self.omit_refined
            ]
        }


class FlakySchemaModel(FakeModel):
    def __init__(self, *, malformed_stage):
        super().__init__()
        self.malformed_stage = malformed_stage
        self.failed_once = False

    def generate(self, prompt, *, session_key, marker):
        if marker == self.malformed_stage and not self.failed_once:
            self.failed_once = True
            if marker == "MAILROOM_PROFILE_JSON":
                value = raw_profile("primary")
                value["domains"] = "invalid-list"
                self.calls.append((prompt, session_key, marker))
                return value
            self.calls.append((prompt, session_key, marker))
            return {"profiles": [raw_profile("primary")]}
        return super().generate(prompt, session_key=session_key, marker=marker)


class MissingOptionalFieldModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.omitted_once = False

    def generate(self, prompt, *, session_key, marker):
        if marker == "MAILROOM_PROFILE_JSON" and not self.omitted_once:
            self.omitted_once = True
            value = raw_profile("primary")
            del value["domains"]
            self.calls.append((prompt, session_key, marker))
            return value
        return super().generate(prompt, session_key=session_key, marker=marker)


class ProviderStatusFailureModel(FakeModel):
    def __init__(self, status, *, fail_marker):
        super().__init__()
        self.status = status
        self.fail_marker = fail_marker

    def generate(self, prompt, *, session_key, marker):
        if marker == self.fail_marker:
            raise ProfileGenerationError(
                f"Anthropic profile API returned HTTP {self.status}"
            )
        return super().generate(prompt, session_key=session_key, marker=marker)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.temp.name) / "profiles")
        self.agents = [
            FleetAgent("primary", Path("/tmp/primary")),
            FleetAgent("research", Path("/tmp/research")),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def generator(self, model=None, provider=None):
        return ResponsibilityProfileGenerator(
            StaticDiscovery(self.agents),
            provider or StaticProvider(),
            model or FakeModel(),
            self.store,
            run_id_factory=lambda: "fixed-run",
        )

    def test_two_pass_generation_covers_fleet_and_preserves_shared_work(self):
        provider = StaticProvider()
        model = FakeModel()
        report = self.generator(model=model, provider=provider).run()
        current = self.store.load_current()
        self.assertEqual(current.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(provider.calls, ["primary", "research"])
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(model.calls[-1][2], "MAILROOM_FLEET_PROFILES_JSON")
        self.assertIn("authoritative current fleet", model.calls[-1][0])
        self.assertEqual(
            current.profile("primary").shared_capabilities, ("financial modeling",)
        )
        self.assertEqual(
            current.profile("primary").sources[0].relative_path, "IDENTITY.md"
        )
        self.assertTrue(report.changed)

    def test_progress_is_metadata_only_and_covers_each_stage(self):
        events = []
        generator = ResponsibilityProfileGenerator(
            StaticDiscovery(self.agents),
            StaticProvider(),
            FakeModel(),
            self.store,
            run_id_factory=lambda: "progress-run",
            progress=events.append,
        )
        generator.run()
        names = [event["event"] for event in events]
        self.assertIn("fleet_discovered", names)
        self.assertEqual(names.count("draft_completed"), 2)
        self.assertEqual(names[-1], "publication_completed")
        rendered = json.dumps(events)
        self.assertNotIn("WORKSPACE SOURCES", rendered)
        self.assertNotIn("Project Redwood", rendered)

    def test_identical_manual_generation_is_idempotent(self):
        first = self.generator().run()
        second = self.generator().run()
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(first.profile_set_id, second.profile_set_id)

    def test_refinement_must_return_every_live_agent(self):
        model = FakeModel(omit_refined="research")
        with self.assertRaisesRegex(ProfileGenerationError, "after 3 attempts"):
            self.generator(model=model).run()
        self.assertIsNone(self.store.load_current(required=False))
        self.assertEqual(len(model.calls), 5)

    def test_safe_provider_status_survives_agent_and_fleet_retry_wrappers(self):
        for marker in (
            "MAILROOM_PROFILE_JSON",
            "MAILROOM_FLEET_PROFILES_JSON",
        ):
            for status in (401, 429, 503):
                with self.subTest(marker=marker, status=status):
                    generator = ResponsibilityProfileGenerator(
                        StaticDiscovery(self.agents),
                        StaticProvider(),
                        ProviderStatusFailureModel(status, fail_marker=marker),
                        self.store,
                        run_id_factory=lambda: "fixed-run",
                        max_model_attempts=1,
                    )
                    with self.assertRaises(ProfileGenerationError) as caught:
                        generator.run()
                    rendered = str(caught.exception)
                    self.assertIn(
                        f"ProfileGenerationError: Anthropic profile API returned HTTP {status}",
                        rendered,
                    )

    def test_unknown_model_error_detail_is_not_retained(self):
        class UnknownFailureModel(FakeModel):
            def generate(self, prompt, *, session_key, marker):
                raise RuntimeError("sensitive unknown failure detail")

        generator = ResponsibilityProfileGenerator(
            StaticDiscovery(self.agents),
            StaticProvider(),
            UnknownFailureModel(),
            self.store,
            run_id_factory=lambda: "fixed-run",
            max_model_attempts=1,
        )
        with self.assertRaises(ProfileGenerationError) as caught:
            generator.run()
        self.assertIn("(RuntimeError)", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    def test_malformed_agent_profile_gets_one_corrective_retry(self):
        model = FlakySchemaModel(malformed_stage="MAILROOM_PROFILE_JSON")
        report = self.generator(model=model).run()
        self.assertEqual(report.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(len(model.calls), 4)
        self.assertTrue(model.calls[1][1].endswith("-retry-2"))
        self.assertIn("SCHEMA CORRECTION", model.calls[1][0])

    def test_missing_optional_array_is_normalized_without_model_retry(self):
        model = MissingOptionalFieldModel()
        report = self.generator(model=model).run()
        self.assertEqual(report.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(self.store.load_current().profile("primary").domains, ())

    def test_malformed_fleet_refinement_gets_one_corrective_retry(self):
        model = FlakySchemaModel(malformed_stage="MAILROOM_FLEET_PROFILES_JSON")
        report = self.generator(model=model).run()
        self.assertEqual(report.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(len(model.calls), 4)
        self.assertTrue(model.calls[-1][1].endswith("-retry-2"))
        self.assertIn("SCHEMA CORRECTION", model.calls[-1][0])

    def test_failed_generation_keeps_last_known_good(self):
        successful = self.generator().run()
        with self.assertRaises(ProfileGenerationError):
            self.generator(model=FakeModel(fail=True)).run()
        self.assertEqual(
            self.store.load_current().profile_set_id, successful.profile_set_id
        )

    def test_stale_override_failure_is_reported_as_generation_failure(self):
        overrides = ProfileOverrideStore(Path(self.temp.name) / "overrides")
        overrides.save("retired", {"agent_id": "retired", "mission": "Own retired work"})
        generator = ResponsibilityProfileGenerator(
            StaticDiscovery(self.agents),
            StaticProvider(),
            FakeModel(),
            self.store,
            run_id_factory=lambda: "fixed-run",
            override_store=overrides,
        )

        with self.assertRaisesRegex(
            ProfileGenerationError,
            "Profile override validation failed: profile override references unknown agent",
        ):
            generator.run()
        self.assertIsNone(self.store.load_current(required=False))

    def test_corpus_provider_is_replaceable_without_generator_changes(self):
        provider = StaticProvider()
        self.generator(provider=provider).run()
        self.assertEqual(provider.calls, ["primary", "research"])
        self.assertEqual(
            self.store.load_current().generation.corpus_provider, "fake-provider"
        )

    def test_missing_workspace_is_reported_but_agent_is_not_dropped(self):
        absent = FleetAgent("primary", Path(self.temp.name) / "absent")
        report = ResponsibilityProfileGenerator(
            StaticDiscovery([absent]),
            DirectWorkspaceCorpusProvider(),
            FakeModel(),
            self.store,
            run_id_factory=lambda: "missing-run",
        ).run()
        self.assertEqual(report.fleet_agent_ids, ("primary",))
        self.assertEqual(report.coverage["primary"]["workspace_status"], "missing")
        self.assertEqual(self.store.load_current().fleet_agent_ids, ("primary",))

    def test_one_provider_failure_is_isolated_without_dropping_agent(self):
        report = self.generator(
            provider=SelectivelyFailingProvider({"research"}),
        ).run()
        self.assertEqual(report.fleet_agent_ids, ("primary", "research"))
        self.assertEqual(report.coverage["research"]["workspace_status"], "provider-error")
        issue = report.coverage["research"]["issues"][0]
        self.assertIn("PermissionError", issue["detail"])
        self.assertNotIn("sensitive", issue["detail"])

    def test_multiple_unavailable_agents_abort_before_replacing_last_good(self):
        successful = self.generator().run()
        with self.assertRaisesRegex(ProfileGenerationError, "Too many"):
            self.generator(
                provider=SelectivelyFailingProvider({"primary", "research"}),
            ).run()
        self.assertEqual(
            self.store.load_current().profile_set_id, successful.profile_set_id
        )


if __name__ == "__main__":
    unittest.main()
