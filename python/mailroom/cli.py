"""Typed command fallback for Mailroom state inspection and decisions."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import (
    DEFAULT_MAILROOM_SECRETS,
    MailroomSecretError,
    env_value,
    mailroom_secret_value,
    resolve_connection,
)
from .catchup import CatchupScanner
from .attachments import MatonAttachmentReader
from .controller import ShadowController, ShadowRunSummary
from .conversation import MatonConversationReader
from .dispatcher import (
    DraftDispatcher,
    OpenClawAgentRunner,
    TelegramCardNotifier,
    TransientAgentError,
    parse_telegram_destinations,
    proposal_violations,
)
from .drafting_policy import DraftingPolicy
from .intake import MatonDeltaIntake
from .ledger import DEFAULT_DB, MailroomLedger
from .models import MailState
from .profile_generation import (
    DEFAULT_PROFILE_MODEL,
    AnthropicSonnetProfileModel,
    DirectWorkspaceCorpusProvider,
    FleetDiscoveryError,
    OpenClawFleetDiscovery,
    ProfileGenerationError,
    ResponsibilityProfileGenerator,
)
from .reconciliation import MatonSentVerifier, SendReconciler
from .reply_guard import MatonSentReplyChecker
from .responsibility_profiles import (
    DEFAULT_PROFILE_OVERRIDE_DIR,
    DEFAULT_PROFILE_DIR,
    FleetProfileSet,
    ProfileOverrideStore,
    ProfileStore,
    ProfileStoreError,
    ProfileValidationError,
    diff_profile_sets,
)
from .router import DeterministicRouter
from .routing_owners import RoutingOwnerPolicy
from .triage import (
    DEFAULT_MIN_OWNER_CONFIDENCE,
    DEFAULT_MIN_OWNER_SEPARATION,
    DEFAULT_TRIAGE_MODEL,
    GeminiFlashLiteTriager,
)


def _print(item) -> None:
    print(json.dumps(item, indent=2, sort_keys=True, default=str))


def _profile_progress(event: dict) -> None:
    """Emit bounded metadata-only progress for cron observability."""
    print(json.dumps(event, sort_keys=True, default=str), file=sys.stderr, flush=True)


def _mailroom_provider_key(name: str, *, required: bool = True) -> str:
    try:
        value = mailroom_secret_value(name)
    except MailroomSecretError as exc:
        raise SystemExit(str(exc)) from None
    if value:
        return value
    if required:
        raise SystemExit(f"{name} is unavailable in {DEFAULT_MAILROOM_SECRETS}")
    return ""


def _review_owners(
    profiles: FleetProfileSet | None,
    router: DeterministicRouter,
    policy: RoutingOwnerPolicy | None = None,
) -> tuple[str, ...]:
    if profiles is not None:
        available = profiles.fleet_agent_ids
    else:
        try:
            available = tuple(
                agent.agent_id for agent in OpenClawFleetDiscovery().discover()
            )
        except Exception:
            available = tuple(dict.fromkeys(pack.agent for pack in router.rulepacks))
    return (policy or RoutingOwnerPolicy.load()).resolve(available)


TRIAGE_DEGRADED_BATCH_FAILURE_THRESHOLD = 3


def _triage_cycle_failed(summary: dict) -> bool:
    """Fail monitoring for availability faults or sustained batch degradation."""
    if int(summary.get("triage_errors", 0)) > 0:
        return True
    degraded = int(summary.get("triage_degraded", 0))
    triaged = int(summary.get("triaged", 0))
    return (
        degraded >= TRIAGE_DEGRADED_BATCH_FAILURE_THRESHOLD
        and degraded >= triaged
    )


def _telegram_destinations(raw: str | None):
    try:
        return parse_telegram_destinations(raw)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailroom", description="Mailroom deterministic control CLI"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    ls = sub.add_parser("list")
    ls.add_argument("--state", choices=[s.value for s in MailState])
    ls.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show")
    show.add_argument("item")
    validate = sub.add_parser(
        "validate-proposal",
        help="Read-only workflow-neutral validation for an approval gate",
    )
    validate.add_argument("item")

    deny = sub.add_parser("deny")
    deny.add_argument("item")
    mute = sub.add_parser("mute-thread")
    mute.add_argument("item")
    mute.add_argument("--reason")
    defer = sub.add_parser("defer")
    defer.add_argument("item")
    defer.add_argument("--hours", type=float, required=True)
    shadow = sub.add_parser(
        "shadow", help="Read-only Maton delta intake and routing; no drafts or alerts"
    )
    shadow.add_argument(
        "--account", required=True, help="Mailbox email in shared/connections.json"
    )
    shadow.add_argument("--connection", help="Explicit Maton connection ID")
    shadow.add_argument("--bootstrap-hours", type=int, default=2)
    shadow.add_argument(
        "--rulepacks", default=str(Path(__file__).with_name("rulepacks"))
    )
    shadow.add_argument("--triage-model", default=DEFAULT_TRIAGE_MODEL)
    _add_profile_routing_arguments(shadow)
    cycle = sub.add_parser(
        "cycle", help="Production intake, route, draft, and approval-card cycle"
    )
    cycle.add_argument("--account", required=True)
    cycle.add_argument("--connection")
    cycle.add_argument("--bootstrap-hours", type=int, default=2)
    cycle.add_argument(
        "--rulepacks", default=str(Path(__file__).with_name("rulepacks"))
    )
    cycle.add_argument("--telegram-chat-id", required=True)
    cycle.add_argument("--telegram-thread-id")
    cycle.add_argument(
        "--telegram-destinations",
        help="JSON object mapping OpenClaw agent ids to {chatId, threadId?}",
    )
    cycle.add_argument(
        "--routing-review-telegram-account-id", default="default",
        help="Telegram account for ambiguous routing-review cards",
    )
    cycle.add_argument(
        "--routing-review-agent-id", default="main",
        help="OpenClaw agent that owns ambiguous routing review",
    )
    cycle.add_argument("--triage-model", default=DEFAULT_TRIAGE_MODEL)
    _add_profile_routing_arguments(cycle)
    catchup = sub.add_parser(
        "catchup",
        help="Dry-run historical routing without moving the live delta checkpoint",
    )
    catchup.add_argument("--account", required=True)
    catchup.add_argument("--connection")
    catchup.add_argument("--hours", type=int, default=96)
    catchup.add_argument(
        "--owners",
        default="",
        help="Comma-separated agent IDs eligible for catch-up routing",
    )
    catchup.add_argument(
        "--rulepacks", default=str(Path(__file__).with_name("rulepacks"))
    )
    catchup.add_argument(
        "--apply-sender",
        action="append",
        default=[],
        help="Exact sender email approved for production promotion; repeat as needed",
    )
    catchup.add_argument(
        "--confirm-count",
        type=int,
        help="Required exact candidate count when --apply-sender is used",
    )
    dispatch = sub.add_parser(
        "dispatch", help="Retry production drafting and card delivery"
    )
    dispatch.add_argument("--account", required=True)
    dispatch.add_argument("--connection")
    dispatch.add_argument(
        "--rulepacks", default=str(Path(__file__).with_name("rulepacks"))
    )
    dispatch.add_argument("--telegram-chat-id", required=True)
    dispatch.add_argument("--telegram-thread-id")
    dispatch.add_argument(
        "--telegram-destinations",
        help="JSON object mapping OpenClaw agent ids to {chatId, threadId?}",
    )
    dispatch.add_argument(
        "--routing-review-telegram-account-id", default="default",
        help="Telegram account for ambiguous routing-review cards",
    )
    dispatch.add_argument(
        "--routing-review-agent-id", default="main",
        help="OpenClaw agent that owns ambiguous routing review",
    )
    dispatch.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    reconcile = sub.add_parser(
        "reconcile",
        help="Recheck accepted or uncertain sends against Outlook Sent Items",
    )
    reconcile.add_argument("--account", required=True)
    reconcile.add_argument("--connection")
    reconcile.add_argument("--limit", type=int, default=20)
    revise = sub.add_parser(
        "revise", help="Apply the operator's revision instructions and send a new card"
    )
    revise.add_argument("item")
    revise.add_argument("instructions")
    revise.add_argument("--account-id", required=True)
    revise.add_argument("--telegram-chat-id", required=True)
    revise.add_argument("--telegram-thread-id")
    revise.add_argument(
        "--telegram-destinations",
        help="JSON object mapping OpenClaw agent ids to {chatId, threadId?}",
    )

    profiles = sub.add_parser(
        "profiles", help="Generate and inspect fleet responsibility profiles"
    )
    profile_sub = profiles.add_subparsers(dest="profile_command", required=True)
    generate = profile_sub.add_parser(
        "generate", help="Generate, refine, validate, and atomically publish"
    )
    generate.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    generate.add_argument("--overrides-dir", default=str(DEFAULT_PROFILE_OVERRIDE_DIR))
    generate.add_argument("--model", default=DEFAULT_PROFILE_MODEL)
    generate.add_argument("--max-file-chars", type=int, default=40_000)
    generate.add_argument("--max-agent-chars", type=int, default=180_000)
    generate.add_argument("--max-files", type=int, default=500)
    generate.add_argument("--max-unavailable-agents", type=int, default=1)
    show_profiles = profile_sub.add_parser(
        "show", help="Show the current or a named profile set"
    )
    show_profiles.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    show_profiles.add_argument("--overrides-dir", default=str(DEFAULT_PROFILE_OVERRIDE_DIR))
    show_profiles.add_argument("agent_id", nargs="?")
    show_profiles.add_argument("--version")
    show_profiles.add_argument("--routing-only", action="store_true")
    show_profiles.add_argument(
        "--raw",
        action="store_true",
        help="Show the stored generated version without applying manual overrides",
    )
    edit_profile = profile_sub.add_parser(
        "edit", help="Create or update a persistent manual override for one agent"
    )
    edit_profile.add_argument("agent_id")
    edit_profile.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    edit_profile.add_argument("--overrides-dir", default=str(DEFAULT_PROFILE_OVERRIDE_DIR))
    edit_profile.add_argument(
        "--set-json",
        help="Merge this JSON object into the existing override",
    )
    edit_profile.add_argument(
        "--replace-json",
        help="Replace the existing override with this JSON object",
    )
    edit_profile.add_argument("--clear", action="store_true")
    edit_profile.add_argument(
        "--publish",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Publish the merged effective profile set after editing",
    )
    validate_profiles = profile_sub.add_parser(
        "validate", help="Validate current profiles plus all manual overrides"
    )
    validate_profiles.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    validate_profiles.add_argument(
        "--overrides-dir", default=str(DEFAULT_PROFILE_OVERRIDE_DIR)
    )
    diff_profiles = profile_sub.add_parser(
        "diff", help="Show a profile diff between versions"
    )
    diff_profiles.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    diff_profiles.add_argument("--previous", required=True)
    diff_profiles.add_argument("--current")
    return parser


def _add_profile_routing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profiles-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument(
        "--min-owner-confidence",
        type=float,
        default=DEFAULT_MIN_OWNER_CONFIDENCE,
    )
    parser.add_argument(
        "--min-owner-separation",
        type=float,
        default=DEFAULT_MIN_OWNER_SEPARATION,
    )


def _load_json_object(raw: str, *, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def _edit_json_in_editor(initial: dict, *, agent_id: str) -> dict:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise SystemExit(
            "No editor configured. Set $EDITOR or use --set-json/--replace-json."
        )
    with tempfile.NamedTemporaryFile(
        "w+", encoding="utf-8", suffix=f"-{agent_id}-mailroom-profile.json"
    ) as handle:
        handle.write(json.dumps(initial, indent=2, sort_keys=True) + "\n")
        handle.flush()
        try:
            command = shlex.split(editor)
            if not command:
                raise SystemExit("Editor command is empty")
            subprocess.run([*command, handle.name], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Editor failed: {exc}") from None
        handle.seek(0)
        return _load_json_object(handle.read(), label="edited override")


def _publish_effective_profiles(
    store: ProfileStore, overrides: ProfileOverrideStore
) -> tuple[FleetProfileSet, object]:
    baseline = _generated_profile_baseline(store, overrides)
    effective = overrides.apply(
        baseline, generated_at=datetime.now(timezone.utc).isoformat()
    )
    publication = store.publish_generated(baseline, effective)
    return effective, publication


def _generated_profile_baseline(
    store: ProfileStore,
    overrides: ProfileOverrideStore,
    *,
    require_persisted_for_overrides: bool = False,
) -> FleetProfileSet:
    baseline = store.load_generated_baseline(required=False)
    if baseline is not None:
        return baseline
    if require_persisted_for_overrides and overrides.list_agent_ids():
        raise ProfileStoreError(
            "Manual overrides exist but no generated baseline is available; "
            "run `mailroom profiles generate` before editing or clearing overrides"
        )
    # Upgrade path for profile stores created before generated baselines existed.
    return store.load_current()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = MailroomLedger(Path(args.db))
    if args.command == "init":
        _print({"ok": True, "db": str(ledger.path)})
    elif args.command == "list":
        state = MailState(args.state) if args.state else None
        _print(ledger.list_items(state=state, limit=args.limit))
    elif args.command == "show":
        item = ledger.get(args.item)
        if item is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        _print(item)
    elif args.command == "validate-proposal":
        item = ledger.get(args.item)
        if item is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        try:
            proposal = json.loads(item.get("proposal_json") or "{}")
        except json.JSONDecodeError:
            proposal = {}
        policy = DraftingPolicy.load()
        violations = proposal_violations(item, proposal, policy=policy)
        _print(
            {
                "ok": not violations,
                "mail_item_id": item["mail_item_id"],
                "quality_gate_version": policy.version,
                "violations": violations,
            }
        )
    elif args.command == "deny":
        item = ledger.get(args.item)
        if item is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        _print(ledger.deny_message(item["mail_item_id"], actor="operator:cli"))
    elif args.command == "mute-thread":
        item = ledger.get(args.item)
        if item is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        ledger.mute_thread(item["mail_item_id"], actor="operator:cli", reason=args.reason)
        _print(
            {
                "ok": True,
                "conversation_id": item["conversation_id"],
                "policy": "MUTED_THREAD",
            }
        )
    elif args.command == "defer":
        item = ledger.get(args.item)
        if item is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        until = (datetime.now(timezone.utc) + timedelta(hours=args.hours)).isoformat()
        _print(ledger.defer(item["mail_item_id"], until, actor="operator:cli"))
    elif args.command == "shadow":
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = args.connection or resolve_connection(args.account)
        intake = MatonDeltaIntake(
            ledger=ledger,
            mailbox=args.account,
            connection_id=connection,
            api_key=api_key,
            bootstrap_lookback_hours=args.bootstrap_hours,
        )
        router = DeterministicRouter.from_directory(args.rulepacks)
        try:
            profiles = ProfileStore(args.profiles_dir).load_current()
        except ProfileStoreError:
            _print(
                {
                    **ShadowRunSummary(triage_errors=1).__dict__,
                    "profile_status": "unavailable",
                }
            )
            return 2
        gemini_key = _mailroom_provider_key("GEMINI_API_KEY", required=False)
        summary = (
            ShadowController(
                ledger,
                intake,
                router,
                triager=GeminiFlashLiteTriager(
                    api_key=gemini_key,
                    profiles=profiles,
                    model=args.triage_model,
                ),
                min_owner_confidence=args.min_owner_confidence,
                min_owner_separation=args.min_owner_separation,
            )
            .run()
            .__dict__
        )
        if not gemini_key:
            # Do not prevent deterministic intake when semantic triage is
            # misconfigured, but make the degraded cycle visible to cron.
            summary["triage_errors"] = max(1, summary["triage_errors"])
        _print(summary)
        if _triage_cycle_failed(summary):
            return 2
    elif args.command == "cycle":
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = args.connection or resolve_connection(args.account)
        intake = MatonDeltaIntake(
            ledger=ledger,
            mailbox=args.account,
            connection_id=connection,
            api_key=api_key,
            bootstrap_lookback_hours=args.bootstrap_hours,
        )
        router = DeterministicRouter.from_directory(args.rulepacks)
        try:
            profiles = ProfileStore(args.profiles_dir).load_current()
        except ProfileStoreError:
            profiles = None
        gemini_key = _mailroom_provider_key("GEMINI_API_KEY", required=False)
        if profiles is None:
            # Preserve the delta checkpoint for a later retry, but continue
            # dispatch/reconciliation for already-persisted work.
            intake_summary = {
                **ShadowRunSummary(triage_errors=1).__dict__,
                "profile_status": "unavailable",
            }
        else:
            intake_summary = (
                ShadowController(
                    ledger,
                    intake,
                    router,
                    run_mode="production",
                    triager=GeminiFlashLiteTriager(
                        api_key=gemini_key,
                        profiles=profiles,
                        model=args.triage_model,
                    ),
                    min_owner_confidence=args.min_owner_confidence,
                    min_owner_separation=args.min_owner_separation,
                )
                .run()
                .__dict__
            )
            if not gemini_key:
                # Dispatch and reconciliation are independent of semantic intake;
                # keep them available while still failing the cycle for monitoring.
                intake_summary["triage_errors"] = max(
                    1, intake_summary["triage_errors"]
                )
        dispatch_summary = (
            DraftDispatcher(
                ledger,
                OpenClawAgentRunner(),
                TelegramCardNotifier(thread_id=args.telegram_thread_id),
                telegram_chat_id=args.telegram_chat_id,
                telegram_thread_id=args.telegram_thread_id,
                telegram_destinations=_telegram_destinations(args.telegram_destinations),
                review_agent_id=args.routing_review_agent_id,
                review_account_id=args.routing_review_telegram_account_id,
                review_owners=_review_owners(profiles, router),
                reply_checker=MatonSentReplyChecker(
                    connection_id=connection, api_key=api_key
                ),
                attachment_reader=MatonAttachmentReader(
                    connection_id=connection, api_key=api_key
                ),
                conversation_reader=MatonConversationReader(
                    connection_id=connection, api_key=api_key
                ),
                mailbox=args.account,
            )
            .run()
            .__dict__
        )
        reconciliation_summary = (
            SendReconciler(
                ledger,
                MatonSentVerifier(connection_id=connection, api_key=api_key),
                mailbox=args.account,
            )
            .run()
            .__dict__
        )
        _print(
            {
                "intake": intake_summary,
                "dispatch": dispatch_summary,
                "reconciliation": reconciliation_summary,
            }
        )
        if _triage_cycle_failed(intake_summary):
            return 2
    elif args.command == "catchup":
        if args.hours <= 0:
            raise SystemExit("--hours must be positive")
        owners = tuple(
            dict.fromkeys(
                owner.strip() for owner in args.owners.split(",") if owner.strip()
            )
        )
        if not owners:
            raise SystemExit("--owners must name at least one agent")
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = args.connection or resolve_connection(args.account)
        intake = MatonDeltaIntake(
            ledger=ledger,
            mailbox=args.account,
            connection_id=connection,
            api_key=api_key,
            bootstrap_lookback_hours=args.hours,
            use_checkpoint=False,
        )
        router = DeterministicRouter.from_directory(args.rulepacks)
        unknown = sorted(set(owners) - {pack.agent for pack in router.rulepacks})
        if unknown:
            raise SystemExit(f"No rulepack found for owner(s): {', '.join(unknown)}")
        if bool(args.apply_sender) != (args.confirm_count is not None):
            raise SystemExit("--apply-sender and --confirm-count must be used together")
        _print(
            CatchupScanner(
                ledger,
                intake,
                router,
                MatonSentReplyChecker(connection_id=connection, api_key=api_key),
                owners=owners,
            ).run(apply_senders=args.apply_sender, confirm_count=args.confirm_count)
        )
    elif args.command == "dispatch":
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = args.connection or resolve_connection(args.account)
        router = DeterministicRouter.from_directory(args.rulepacks)
        try:
            profiles = ProfileStore(args.profiles_dir).load_current()
        except ProfileStoreError:
            profiles = None
        dispatch_only_summary = DraftDispatcher(
            ledger,
            OpenClawAgentRunner(),
            TelegramCardNotifier(thread_id=args.telegram_thread_id),
            telegram_chat_id=args.telegram_chat_id,
            telegram_thread_id=args.telegram_thread_id,
            telegram_destinations=_telegram_destinations(args.telegram_destinations),
            review_agent_id=args.routing_review_agent_id,
            review_account_id=args.routing_review_telegram_account_id,
            review_owners=_review_owners(profiles, router),
            reply_checker=MatonSentReplyChecker(
                connection_id=connection, api_key=api_key
            ),
            attachment_reader=MatonAttachmentReader(
                connection_id=connection, api_key=api_key
            ),
            conversation_reader=MatonConversationReader(
                connection_id=connection, api_key=api_key
            ),
            mailbox=args.account,
        ).run()
        _print(dispatch_only_summary.__dict__)
    elif args.command == "reconcile":
        if args.limit <= 0:
            raise SystemExit("--limit must be positive")
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = args.connection or resolve_connection(args.account)
        summary = SendReconciler(
            ledger,
            MatonSentVerifier(connection_id=connection, api_key=api_key),
            mailbox=args.account,
        ).run(limit=args.limit)
        _print(summary.__dict__)
        if summary.errors:
            return 2
    elif args.command == "revise":
        target = ledger.get(args.item)
        if target is None:
            raise SystemExit(f"Mail item not found: {args.item}")
        api_key = env_value("MATON_API_KEY")
        if not api_key:
            raise SystemExit(
                "MATON_API_KEY is unavailable in the environment or ~/.openclaw/.env"
            )
        connection = resolve_connection(target["mailbox"])
        dispatcher = DraftDispatcher(
            ledger,
            OpenClawAgentRunner(),
            TelegramCardNotifier(thread_id=args.telegram_thread_id),
            telegram_chat_id=args.telegram_chat_id,
            telegram_thread_id=args.telegram_thread_id,
            telegram_destinations=_telegram_destinations(args.telegram_destinations),
            reply_checker=MatonSentReplyChecker(
                connection_id=connection, api_key=api_key
            ),
            attachment_reader=MatonAttachmentReader(
                connection_id=connection, api_key=api_key
            ),
            conversation_reader=MatonConversationReader(
                connection_id=connection, api_key=api_key
            ),
            mailbox=target["mailbox"],
        )
        try:
            item = dispatcher.revise(
                args.item,
                args.instructions,
                account_id=args.account_id,
                chat_id=args.telegram_chat_id,
            )
        except TransientAgentError as exc:
            # The revision stayed pending, so report a retryable outcome rather
            # than a hard failure; the operator can reply to the same prompt again.
            _print(
                {
                    "ok": False,
                    "retryable": True,
                    "mail_item_id": target["mail_item_id"],
                    "state": MailState.REVISION_REQUESTED.value,
                    "error": str(exc),
                }
            )
            return 0
        _print(
            {
                "ok": True,
                "mail_item_id": item["mail_item_id"],
                "state": item["state"],
                "suppressed": item["state"] == MailState.REPLIED_ELSEWHERE.value,
            }
        )
    elif args.command == "profiles":
        store = ProfileStore(args.profiles_dir)
        if args.profile_command == "generate":
            try:
                report = ResponsibilityProfileGenerator(
                    OpenClawFleetDiscovery(),
                    DirectWorkspaceCorpusProvider(
                        max_file_chars=args.max_file_chars,
                        max_agent_chars=args.max_agent_chars,
                        max_files=args.max_files,
                    ),
                    AnthropicSonnetProfileModel(
                        api_key=_mailroom_provider_key("ANTHROPIC_API_KEY"),
                        model_id=args.model,
                    ),
                    store,
                    max_unavailable_agents=args.max_unavailable_agents,
                    progress=_profile_progress,
                    override_store=ProfileOverrideStore(args.overrides_dir),
                ).run()
            except (FleetDiscoveryError, ProfileGenerationError) as exc:
                _print(
                    {
                        "ok": False,
                        "error": (
                            "fleet_discovery_failed"
                            if isinstance(exc, FleetDiscoveryError)
                            else "profile_generation_failed"
                        ),
                        "message": str(exc),
                    }
                )
                return 2
            # Summary includes coverage/status counts and paths, never corpus contents.
            _print(report.summary())
        elif args.profile_command == "show":
            overrides = ProfileOverrideStore(args.overrides_dir)
            if args.version:
                profile_set = store.load_version(args.version)
            else:
                baseline = _generated_profile_baseline(store, overrides)
                profile_set = baseline if args.raw else overrides.apply(baseline)
            if args.agent_id:
                try:
                    profile = profile_set.profile(args.agent_id)
                except KeyError:
                    _print(
                        {
                            "ok": False,
                            "error": "unknown_profile_agent",
                            "message": f"No responsibility profile for agent: {args.agent_id}",
                        }
                    )
                    return 2
                _print(
                    profile.to_dict(routing_only=args.routing_only)
                    if args.routing_only
                    else profile.to_dict()
                )
                return 0
            _print(
                profile_set.routing_payload()
                if args.routing_only
                else profile_set.to_dict()
            )
        elif args.profile_command == "edit":
            if sum(bool(item) for item in (args.set_json, args.replace_json, args.clear)) > 1:
                raise SystemExit("Choose only one of --set-json, --replace-json, or --clear")
            overrides = ProfileOverrideStore(args.overrides_dir)
            try:
                baseline = _generated_profile_baseline(
                    store, overrides, require_persisted_for_overrides=True
                )
                agent_id = baseline.profile(args.agent_id).agent_id
                if store.load_generated_baseline(required=False) is None:
                    # One-time upgrade of pre-baseline stores. This does not
                    # change effective routing content.
                    store.publish_generated(baseline, store.load_current())
            except KeyError:
                _print(
                    {
                        "ok": False,
                        "error": "unknown_profile_agent",
                        "message": f"No responsibility profile for agent: {args.agent_id}",
                    }
                )
                return 2
            except (ProfileValidationError, ProfileStoreError) as exc:
                _print(
                    {
                        "ok": False,
                        "error": "profile_edit_failed",
                        "message": str(exc),
                    }
                )
                return 2
            previous_override = overrides.snapshot(agent_id)
            try:
                if args.clear:
                    overrides.delete(agent_id)
                    saved = {"agent_id": agent_id}
                elif args.replace_json:
                    saved = overrides.save(
                        agent_id,
                        _load_json_object(args.replace_json, label="--replace-json"),
                    )
                elif args.set_json:
                    current_override = overrides.load(agent_id)
                    current_override.update(
                        _load_json_object(args.set_json, label="--set-json")
                    )
                    saved = overrides.save(agent_id, current_override)
                else:
                    saved = overrides.save(
                        agent_id,
                        _edit_json_in_editor(
                            overrides.load(agent_id), agent_id=agent_id
                        ),
                    )
                publication_result = (
                    _publish_effective_profiles(store, overrides)
                    if args.publish
                    else None
                )
            except (ProfileValidationError, ProfileStoreError, OSError) as exc:
                if args.publish:
                    overrides.restore(agent_id, previous_override)
                _print(
                    {
                        "ok": False,
                        "error": "profile_edit_failed",
                        "message": str(exc),
                    }
                )
                return 2
            result = {
                "ok": True,
                "agent_id": saved["agent_id"],
                "override_path": str(overrides.path_for(saved["agent_id"])),
                "override_fields": sorted(set(saved) - {"agent_id"}),
            }
            if args.publish:
                assert publication_result is not None
                effective, publication = publication_result
                result.update(
                    {
                        "published": True,
                        "profile_set_id": effective.profile_set_id,
                        "changed": publication.changed,
                        "diff_path": str(publication.diff_path)
                        if publication.diff_path
                        else None,
                    }
                )
            else:
                result["published"] = False
            _print(result)
        elif args.profile_command == "validate":
            overrides = ProfileOverrideStore(args.overrides_dir)
            try:
                baseline = _generated_profile_baseline(store, overrides)
                effective = overrides.apply(baseline)
            except (ProfileValidationError, ProfileStoreError) as exc:
                _print(
                    {
                        "ok": False,
                        "error": "override_validation_failed",
                        "message": str(exc),
                    }
                )
                return 2
            _print(
                {
                    "ok": True,
                    "profile_set_id": effective.profile_set_id,
                    "base_profile_set_id": baseline.profile_set_id,
                    "changed_by_overrides": effective.profile_set_id != baseline.profile_set_id,
                    "override_agent_ids": overrides.list_agent_ids(),
                    "fleet_agent_ids": effective.fleet_agent_ids,
                }
            )
        elif args.profile_command == "diff":
            previous = store.load_version(args.previous)
            current = (
                store.load_version(args.current)
                if args.current
                else store.load_current()
            )
            _print(diff_profile_sets(previous, current).to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
