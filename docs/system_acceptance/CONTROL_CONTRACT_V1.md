# System Acceptance Control Contract V1

Control Plane integration must import only the canonical owner surface:

```python
from modular_system_acceptance.control_contract import (
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceRunSnapshot,
    PinnedSystemAcceptanceHttpServer,
    SystemAcceptanceOwner,
)
```

The canonical surface intentionally exports `PinnedSystemAcceptanceHttpServer`, not the low-level base transport. Exact full-RC SHA is trusted process composition, execution mode is fixed to PAPER, and browser JSON cannot override either value.

Production composition must use `build_complete_acceptance_registry()` so every non-soak phase has an explicit owner-native executor and all soak phases use the checkpointed soak executor. A partial executor mapping is `PHASE_BLOCKED` and must not be advertised by Control UI as a runnable full acceptance capability.
