from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    PAPER = "paper"


class RuntimeState(StrEnum):
    CREATED = "created"
    CONFIGURING = "configuring"
    CONFIGURED = "configured"
    INITIALIZING = "initializing"
    RECOVERING = "recovering"
    RECONCILING = "reconciling"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    DISPOSED = "disposed"


class HealthStatus(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"
    STOPPING = "stopping"
    STOPPED = "stopped"


class KillSwitchScope(StrEnum):
    GLOBAL = "global"
    PORTFOLIO = "portfolio"


class PlanRiskClass(StrEnum):
    RISK_INCREASING = "risk_increasing"
    RISK_REDUCING = "risk_reducing"
    NEUTRAL = "neutral"
