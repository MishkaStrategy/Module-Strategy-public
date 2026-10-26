from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import uvicorn

from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService
from modular_trading_runner import RuntimeEventJournal, TradingRunnerOperationalEventQuery
from modular_trading_runner.operational_events_http import TradingRunnerOperationalEventsHttpServer

OWNER_READER = "events-owner-reader-0123456789abcdef"
PRIVATE_PATH = "/Users/private/runtime-events.log"
PRIVATE_TOKEN = "BROWSER-PRIVATE-EVENT-TOKEN"
NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
RESOURCE = "browser-paper-runtime"


def _append(
    journal: RuntimeEventJournal,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> None:
    journal.append(
        timestamp=NOW + timedelta(seconds=sequence),
        event_type=event_type,
        aggregate_id=RESOURCE,
        payload=payload,
    )


def build_owner_query() -> TradingRunnerOperationalEventQuery:
    journal = RuntimeEventJournal(run_id="browser-structured-events-e2e")
    _append(journal, 1, "runtime.started", {"state": "running"})
    _append(
        journal,
        2,
        "operator.lifecycle",
        {
            "operation": "START",
            "request_id": "browser-event-start-1",
            "operator": "control-plane:local-operator",
            "result": "SUCCEEDED",
            "previous_state": "configured",
            "new_state": "running",
            "config_hash": "a" * 64,
            "replayed": False,
        },
    )
    _append(
        journal,
        3,
        "runtime.failed",
        {
            "error_type": "BootstrapError",
            "error": f"{PRIVATE_PATH} token={PRIVATE_TOKEN}",
        },
    )
    for sequence in range(4, 101):
        _append(journal, sequence, "runtime.stopped", {})
    _append(
        journal,
        101,
        "kill_switch.activated",
        {
            "scope": "global",
            "cancellation_pending": False,
            "reason": f"{PRIVATE_PATH} token={PRIVATE_TOKEN}",
        },
    )
    _append(
        journal,
        102,
        "kill_switch.reset",
        {"reason": f"{PRIVATE_PATH} token={PRIVATE_TOKEN}"},
    )
    return TradingRunnerOperationalEventQuery(journal, resource=RESOURCE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reader-token", required=True)
    args = parser.parse_args()

    owner = TradingRunnerOperationalEventsHttpServer(
        build_owner_query(),
        reader_token=OWNER_READER,
        port=0,
    )
    owner.start()
    owner_host, owner_port = owner.address
    settings = ServerSettings(
        reader_token=args.reader_token,
        trading_runner_events_url=f"http://{owner_host}:{owner_port}",
        trading_runner_events_reader_token=OWNER_READER,
    )
    app = create_server(settings, service=ControlPlaneService())
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        owner.close()


if __name__ == "__main__":
    main()
