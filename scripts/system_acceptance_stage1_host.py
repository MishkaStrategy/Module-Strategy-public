"""Mac-local Stage-1 service host for already-authoritative owner objects.

This module is deliberately not a CLI. A trusted deployment/service-manager context
must first materialize ``Stage1DeploymentComposition`` from authoritative owner
objects. The host only owns the durable System Acceptance process lifecycle around
that frozen composition.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from modular_system_acceptance import ProductionSystemAcceptanceProcess
from scripts.system_acceptance_stage1_deployment import Stage1DeploymentComposition

_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Stage1MacServiceConfig:
    """Deployment-private process settings plus the safe exact candidate identity."""

    candidate_sha: str
    state_path: Path = field(repr=False)
    reader_token: str = field(repr=False)
    operator_token: str = field(repr=False)
    instance_id: str = "system-acceptance-owner"
    port: int = 8781
    poll_seconds: float = 1.0
    lease_seconds: int = 30
    operator_subject: str = "control-plane:local-operator"
    host_wait_seconds: float = 0.5

    def __post_init__(self) -> None:
        _git_sha(self.candidate_sha, "candidate_sha")
        if not isinstance(self.state_path, Path):
            raise TypeError("state_path must be Path")
        normalized = self.state_path.expanduser()
        if not normalized.is_absolute():
            raise ValueError("state_path must be absolute")
        if normalized.is_symlink():
            raise ValueError("state_path must not be a symlink")
        object.__setattr__(self, "state_path", normalized)
        _token(self.reader_token, "reader_token")
        _token(self.operator_token, "operator_token")
        if self.reader_token == self.operator_token:
            raise ValueError("reader_token and operator_token must be distinct")
        _identifier(self.instance_id, "instance_id")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("port must be between 0 and 65535")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, (int, float))
            or not 0.05 <= float(self.poll_seconds) <= 60.0
        ):
            raise ValueError("poll_seconds must be between 0.05 and 60")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 5 <= self.lease_seconds <= 300
        ):
            raise ValueError("lease_seconds must be between 5 and 300")
        _identifier(self.operator_subject, "operator_subject")
        if (
            isinstance(self.host_wait_seconds, bool)
            or not isinstance(self.host_wait_seconds, (int, float))
            or not 0.05 <= float(self.host_wait_seconds) <= 60.0
        ):
            raise ValueError("host_wait_seconds must be between 0.05 and 60")


@dataclass(frozen=True, slots=True)
class Stage1PrestartProof:
    """Sanitized immutable identity proof safe to persist in deployment evidence."""

    candidate_sha: str
    full_rc_sha: str
    config_digest: str
    dataset_id: str
    dataset_digest: str
    dataset_version: int
    dataset_timeframe: str
    strategy_id: str
    strategy_package_hash: str
    trading_runner_config_digest: str
    trading_runner_runtime_id: str
    trading_runner_portfolio_id: str
    research_experiment_id: str
    research_specification_fingerprint: str
    execution_mode: str = "paper"
    allow_soak_24h: bool = False
    allow_soak_72h: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        _git_sha(self.candidate_sha, "candidate_sha")
        _git_sha(self.full_rc_sha, "full_rc_sha")
        for value, name in (
            (self.config_digest, "config_digest"),
            (self.dataset_digest, "dataset_digest"),
            (self.strategy_package_hash, "strategy_package_hash"),
            (self.trading_runner_config_digest, "trading_runner_config_digest"),
            (
                self.research_specification_fingerprint,
                "research_specification_fingerprint",
            ),
        ):
            _sha256(value, name)
        for value, name in (
            (self.dataset_id, "dataset_id"),
            (self.dataset_timeframe, "dataset_timeframe"),
            (self.strategy_id, "strategy_id"),
            (self.trading_runner_runtime_id, "trading_runner_runtime_id"),
            (self.trading_runner_portfolio_id, "trading_runner_portfolio_id"),
            (self.research_experiment_id, "research_experiment_id"),
        ):
            _identifier(value, name)
        if (
            isinstance(self.dataset_version, bool)
            or not isinstance(self.dataset_version, int)
            or self.dataset_version <= 0
        ):
            raise ValueError("dataset_version must be a positive integer")
        if self.execution_mode != "paper":
            raise ValueError("Stage-1 execution_mode must be paper")
        if not isinstance(self.allow_soak_24h, bool) or not isinstance(self.allow_soak_72h, bool):
            raise TypeError("Stage-1 soak policy flags must be bool")
        if self.allow_soak_24h or self.allow_soak_72h:
            raise ValueError("Stage-1 24h and 72h soak policies must remain disabled")
        if self.schema_version != 1:
            raise ValueError("unsupported Stage-1 pre-start proof schema_version")

    def mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "status": "READY_FOR_LOCAL_START",
                "candidate_sha": self.candidate_sha,
                "full_rc_sha": self.full_rc_sha,
                "config_digest": self.config_digest,
                "dataset": {
                    "dataset_id": self.dataset_id,
                    "dataset_digest": self.dataset_digest,
                    "version": self.dataset_version,
                    "timeframe": self.dataset_timeframe,
                },
                "strategy": {
                    "strategy_id": self.strategy_id,
                    "package_hash": self.strategy_package_hash,
                },
                "trading_runner": {
                    "config_digest": self.trading_runner_config_digest,
                    "runtime_id": self.trading_runner_runtime_id,
                    "portfolio_id": self.trading_runner_portfolio_id,
                    "execution_mode": self.execution_mode,
                },
                "research": {
                    "experiment_id": self.research_experiment_id,
                    "specification_fingerprint": self.research_specification_fingerprint,
                },
                "policy": {
                    "allow_soak_24h": self.allow_soak_24h,
                    "allow_soak_72h": self.allow_soak_72h,
                },
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            dict(self.mapping()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


def build_stage1_prestart_proof(
    composition: Stage1DeploymentComposition,
    *,
    candidate_sha: str,
) -> Stage1PrestartProof:
    """Project one materialized composition into path/token-free pre-start evidence."""

    if not isinstance(composition, Stage1DeploymentComposition):
        raise TypeError("composition must be Stage1DeploymentComposition")
    _git_sha(candidate_sha, "candidate_sha")
    material = composition.material
    return Stage1PrestartProof(
        candidate_sha=candidate_sha,
        full_rc_sha=material.full_rc_sha,
        config_digest=material.config_digest,
        dataset_id=material.dataset_id,
        dataset_digest=material.dataset_digest,
        dataset_version=material.dataset_version,
        dataset_timeframe=material.dataset_timeframe,
        strategy_id=material.strategy_id,
        strategy_package_hash=material.strategy_package_hash,
        trading_runner_config_digest=material.trading_runner_config_digest,
        trading_runner_runtime_id=material.trading_runner_runtime_id,
        trading_runner_portfolio_id=material.trading_runner_portfolio_id,
        research_experiment_id=material.research_experiment_id,
        research_specification_fingerprint=material.research_specification_fingerprint,
    )


class Stage1MacServiceHost:
    """Lifecycle host embedded by one trusted Mac-local service-manager process.

    The host never constructs or discovers owner objects. Its caller must pass an
    already-materialized composition created from the authoritative Market Data,
    Strategy Factory, Backtest, Research, and live PAPER Trading Runner owners.
    """

    def __init__(
        self,
        *,
        composition: Stage1DeploymentComposition,
        config: Stage1MacServiceConfig,
    ) -> None:
        if not isinstance(composition, Stage1DeploymentComposition):
            raise TypeError("composition must be Stage1DeploymentComposition")
        if not isinstance(config, Stage1MacServiceConfig):
            raise TypeError("config must be Stage1MacServiceConfig")
        self.composition = composition
        self.config = config
        self.prestart_proof = build_stage1_prestart_proof(
            composition,
            candidate_sha=config.candidate_sha,
        )
        self._process: ProductionSystemAcceptanceProcess | None = None
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the durable owner/loopback service exactly once."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Stage-1 Mac service host is closed")
            if self._process is not None:
                if not self._process.running:
                    raise RuntimeError("System Acceptance process stopped unexpectedly")
                return
            process = self.composition.build_process(
                state_path=self.config.state_path,
                reader_token=self.config.reader_token,
                operator_token=self.config.operator_token,
                instance_id=self.config.instance_id,
                port=self.config.port,
                poll_seconds=self.config.poll_seconds,
                lease_seconds=self.config.lease_seconds,
                operator_subject=self.config.operator_subject,
            )
            try:
                process.start()
                if not process.running:
                    raise RuntimeError("System Acceptance process did not enter running state")
                host, _ = process.address
                if host not in _LOOPBACK_HOSTS:
                    raise RuntimeError("System Acceptance process escaped loopback")
            except BaseException:
                process.close()
                raise
            self._process = process

    def run(self, stop_event: threading.Event) -> None:
        """Run under a service-manager-owned lifetime; the event is not a soak clock."""

        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be threading.Event")
        self.start()
        try:
            while not stop_event.wait(self.config.host_wait_seconds):
                if not self.running:
                    raise RuntimeError("System Acceptance process stopped unexpectedly")
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None
        if process is not None:
            process.close()

    @property
    def running(self) -> bool:
        process = self._process
        return bool(process is not None and process.running)

    @property
    def address(self) -> tuple[str, int] | None:
        process = self._process
        if process is None:
            return None
        return process.address

    def __enter__(self) -> Stage1MacServiceHost:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _git_sha(value: str, name: str) -> None:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase 40-character Git SHA")


def _sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _INSTANCE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a portable identifier")


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")


__all__ = [
    "Stage1MacServiceConfig",
    "Stage1MacServiceHost",
    "Stage1PrestartProof",
    "build_stage1_prestart_proof",
]
