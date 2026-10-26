from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
)
from modular_system_acceptance.trusted_http import PinnedSystemAcceptanceHttpServer


class DeterministicExecutor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> int:
    port = int(required_environment("MODULAR_ACCEPTANCE_E2E_PORT"))
    state_path = Path(required_environment("MODULAR_ACCEPTANCE_E2E_STATE_PATH"))
    full_rc_sha = required_environment("MODULAR_ACCEPTANCE_E2E_FULL_RC_SHA")
    reader_token = required_environment("MODULAR_ACCEPTANCE_E2E_READER_TOKEN")
    operator_token = required_environment("MODULAR_ACCEPTANCE_E2E_OPERATOR_TOKEN")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteAcceptanceStore(state_path)
    executor = DeterministicExecutor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="control-ui-e2e-owner",
        lease_seconds=5,
    )
    server = PinnedSystemAcceptanceHttpServer(
        owner,
        full_rc_sha=full_rc_sha,
        reader_token=reader_token,
        operator_token=operator_token,
        host="127.0.0.1",
        port=port,
    )

    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    print(f"System Acceptance E2E owner listening on 127.0.0.1:{port}", flush=True)
    try:
        stopping.wait()
    finally:
        server.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
