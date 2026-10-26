from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from .cli import main as legacy_main
from .client import ClientSettings, ControlClientError
from .notifications import NotificationStatus, TestNotificationRequest, TestNotificationResult
from .notifications_client import NotificationsControlClient
from .operational_events import OperationalEventPage, OperationalEventSeverity
from .operational_events_client import OperationalEventsControlClient
from .paper_lifecycle import (
    PaperLifecycleMutation,
    PaperLifecycleMutationRequest,
    PaperLifecycleMutationResult,
)
from .paper_lifecycle_client import PaperLifecycleControlClient

_EXIT_BY_OWNER_STATUS = {"ok": 0, "degraded": 2, "unavailable": 3}
_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"--url", "--timeout"})
_LIFECYCLE_COMMANDS = {
    "start": PaperLifecycleMutation.START,
    "stop": PaperLifecycleMutation.STOP,
    "kill": PaperLifecycleMutation.ACTIVATE_KILL_SWITCH,
    "reset": PaperLifecycleMutation.RESET_KILL_SWITCH,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = _command(arguments)
    if command not in {"notifications", "events", "paper-lifecycle"}:
        return legacy_main(arguments)
    parser = _build_parser()
    args = parser.parse_args(arguments)
    try:
        settings = ClientSettings(
            base_url=args.url,
            reader_token=_optional_secret(os.environ.get("MODULAR_CONTROL_READER_TOKEN")),
            operator_token=_optional_secret(os.environ.get("MODULAR_CONTROL_OPERATOR_TOKEN")),
            timeout_seconds=args.timeout,
        )
        if command == "notifications":
            return _run_notifications(args, settings=settings)
        if command == "events":
            return _run_events(args, settings=settings)
        return _run_paper_lifecycle(args, settings=settings)
    except (ControlClientError, ValueError, TypeError) as exc:
        return _print_error(exc, as_json=bool(getattr(args, "json", False)))


def _run_notifications(args: argparse.Namespace, *, settings: ClientSettings) -> int:
    client = NotificationsControlClient(settings)
    if args.notifications_command == "status":
        status = client.notifications_status()
        _print_status(status, as_json=args.json)
        return _EXIT_BY_OWNER_STATUS[status.status]
    if args.notifications_command == "test":
        result = client.notifications_test(TestNotificationRequest(request_id=args.request_id))
        _print_test_result(result, as_json=args.json)
        return 0
    raise ValueError(f"unsupported notifications command: {args.notifications_command}")


def _run_events(args: argparse.Namespace, *, settings: ClientSettings) -> int:
    client = OperationalEventsControlClient(settings)
    page = client.operational_events(
        cursor=args.cursor,
        limit=args.limit,
        event_types=tuple(args.event_type or ()),
        severities=tuple(OperationalEventSeverity(value) for value in (args.severity or ())),
    )
    _print_events(page, as_json=args.json)
    return 0


def _run_paper_lifecycle(args: argparse.Namespace, *, settings: ClientSettings) -> int:
    operation = _LIFECYCLE_COMMANDS.get(args.paper_lifecycle_command)
    if operation is None:
        raise ValueError(f"unsupported PAPER lifecycle command: {args.paper_lifecycle_command}")
    client = PaperLifecycleControlClient(settings)
    result = client.paper_lifecycle_mutate(
        operation,
        PaperLifecycleMutationRequest(request_id=args.request_id),
    )
    _print_paper_lifecycle_result(result, as_json=args.json)
    return 0


def _command(arguments: Sequence[str]) -> str | None:
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in _GLOBAL_OPTIONS_WITH_VALUE:
            skip_value = True
            continue
        if any(argument.startswith(f"{option}=") for option in _GLOBAL_OPTIONS_WITH_VALUE):
            continue
        if argument.startswith("-"):
            continue
        return argument
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modular-control")
    parser.add_argument(
        "--url",
        default=os.environ.get("MODULAR_CONTROL_URL", "http://127.0.0.1:8765"),
        help="Loopback Control Plane base URL.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("MODULAR_CONTROL_TIMEOUT_SECONDS", "2.0")),
        help="Monitoring request timeout in seconds (0.05..30).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_notifications_parser(commands)
    _add_events_parser(commands)
    _add_paper_lifecycle_parser(commands)
    return parser


def _add_notifications_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    notifications = commands.add_parser(
        "notifications",
        help="Inspect Signal Bot owner status or send the fixed safe test notification.",
    )
    notification_commands = notifications.add_subparsers(
        dest="notifications_command",
        required=True,
    )
    status = notification_commands.add_parser(
        "status",
        help="Show the bounded Signal Bot owner status projection.",
    )
    _add_json_flag(status)
    test = notification_commands.add_parser(
        "test",
        help="Send the owner-defined fixed test notification.",
    )
    test.add_argument(
        "--request-id",
        required=True,
        help="Bounded idempotency request ID; reuse only to replay the identical test request.",
    )
    _add_json_flag(test)


def _add_events_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    events = commands.add_parser(
        "events",
        help="Read bounded structured operational events from the Trading Runner owner.",
    )
    events.add_argument(
        "--cursor", default=None, help="Opaque owner cursor from the previous page."
    )
    events.add_argument("--limit", type=int, default=100, help="Page size between 1 and 200.")
    events.add_argument(
        "--event-type",
        action="append",
        default=[],
        help="Accepted owner event type. Repeat for multiple values.",
    )
    events.add_argument(
        "--severity",
        action="append",
        choices=[value.value for value in OperationalEventSeverity],
        default=[],
        help="Severity filter. Repeat for multiple values.",
    )
    _add_json_flag(events)


def _add_paper_lifecycle_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    lifecycle = commands.add_parser(
        "paper-lifecycle",
        help="Run owner-native PAPER-only Trading Runner lifecycle mutations.",
    )
    lifecycle_commands = lifecycle.add_subparsers(dest="paper_lifecycle_command", required=True)
    for name, help_text in (
        ("start", "Start the authoritative PAPER runtime."),
        ("stop", "Stop the authoritative PAPER runtime."),
        ("kill", "Activate the authoritative PAPER kill switch."),
        ("reset", "Reset/recover the authoritative PAPER kill switch."),
    ):
        command = lifecycle_commands.add_parser(name, help=help_text)
        command.add_argument(
            "--request-id",
            required=True,
            help="Bounded owner idempotency request ID; reuse only for the identical operation.",
        )
        _add_json_flag(command)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit the versioned JSON contract.")


def _print_status(status: NotificationStatus, *, as_json: bool) -> None:
    if as_json:
        print(status.model_dump_json(indent=2))
        return
    print(f"STATUS        {status.status.upper()}")
    print(f"CONFIGURED    {status.configured}")
    print(f"REACHABLE     {status.reachable}")
    print(f"ENABLED       {status.notifications_enabled}")
    print(f"CYCLES        {status.cycles}")
    print(f"SIGNALS_SENT  {status.signals_sent}")
    print(
        "LAST_SUCCESS  "
        + (status.last_success_at.isoformat() if status.last_success_at is not None else "—")
    )
    print(f"LAST_ERROR    {status.last_error_type or '—'}")


def _print_test_result(result: TestNotificationResult, *, as_json: bool) -> None:
    if as_json:
        print(result.model_dump_json(indent=2))
        return
    print(f"REQUEST    {result.request_id}")
    print(f"OPERATOR   {result.operator}")
    print(f"REPLAYED   {result.replayed}")
    print(f"COMPLETED  {result.completed_at.isoformat()}")


def _print_events(page: OperationalEventPage, *, as_json: bool) -> None:
    if as_json:
        print(page.model_dump_json(indent=2))
        return
    print(f"SOURCE      {page.source}")
    print(f"EVENTS      {len(page.events)}")
    print(f"HAS_MORE    {page.has_more}")
    print(f"NEXT_CURSOR {page.next_cursor}")
    for event in page.events:
        state = event.state or "—"
        print(
            f"{event.timestamp.isoformat()} {event.severity.value:<7} "
            f"{event.event_type} {event.resource} state={state}"
        )
        if event.payload:
            print(f"  payload={json.dumps(event.payload, sort_keys=True, ensure_ascii=False)}")


def _print_paper_lifecycle_result(
    result: PaperLifecycleMutationResult,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(result.model_dump_json(indent=2))
        return
    print(f"REQUEST    {result.request_id}")
    print(f"OPERATION  {result.mutation.value}")
    print(f"OPERATOR   {result.operator}")
    print(f"STATE      {result.previous_state} -> {result.new_state}")
    print(f"REPLAYED   {result.replayed}")
    print(f"COMPLETED  {result.completed_at.isoformat()}")


def _print_error(error: Exception, *, as_json: bool) -> int:
    if isinstance(error, ControlClientError):
        code = error.code
        message = error.message
        action = error.action
        status_code = error.status_code
    else:
        code = "CONTROL_CONFIGURATION_INVALID"
        message = str(error)
        action = "Check the local Control Plane URL, command arguments and timeout configuration."
        status_code = None
    payload: dict[str, object] = {
        "code": code,
        "message": message,
        "action": action,
        "status_code": status_code,
    }
    if as_json:
        print(json.dumps({"error": payload}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR {code}: {message}", file=sys.stderr)
        if action:
            print(f"ACTION: {action}", file=sys.stderr)
    return 4


def _optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


__all__ = ["main"]
