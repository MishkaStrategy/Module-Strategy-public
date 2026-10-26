from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .contracts import AcceptanceErrorCode, AcceptanceOwnerError
from .production_identity import TrustedAcceptanceIdentity, require_identifier, require_sha256

CONFIG_IDENTITY_SCHEMA_VERSION = "1"
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ALLOWED_VALUATION_POLICIES = frozenset({"last", "mark"})


@dataclass(frozen=True, slots=True, order=True)
class OwnerContractIdentity:
    """Safe version identity for one authoritative owner contract."""

    owner: str
    version: str

    def __post_init__(self) -> None:
        require_identifier(self.owner, "owner contract name")
        require_identifier(self.version, "owner contract version")


STAGE1_OWNER_CONTRACTS_V1 = tuple(
    sorted(
        (
            OwnerContractIdentity("backtest", "application-v1"),
            OwnerContractIdentity("market_data", "registered-dataset-v1"),
            OwnerContractIdentity("research", "prepared-experiment-v1"),
            OwnerContractIdentity("strategy_factory", "strategy-package-v1"),
            OwnerContractIdentity("system_acceptance", "production-bootstrap-v1"),
            OwnerContractIdentity("trading_runner", "paper-runtime-v1"),
        )
    )
)
REQUIRED_OWNER_CONTRACTS = frozenset(item.owner for item in STAGE1_OWNER_CONTRACTS_V1)


@dataclass(frozen=True, slots=True)
class AcceptanceConfigurationMaterial:
    """Versioned safe material used to derive the deployment-owned config digest.

    The material intentionally contains semantic identities only. Filesystem roots,
    bearer tokens, credentials and runtime objects belong to the deployment boundary
    and must never be embedded here.
    """

    full_rc_sha: str
    dataset_id: str
    dataset_digest: str
    dataset_version: int
    dataset_timeframe: str
    strategy_id: str
    strategy_package_hash: str
    backtest_config_digest: str
    backtest_performance_digest: str
    valuation_policy: str
    funding_identity_digest: str | None
    mark_identity_digest: str | None
    trading_runner_config_digest: str
    trading_runner_runtime_id: str
    trading_runner_portfolio_id: str
    research_experiment_id: str
    research_specification_fingerprint: str
    owner_contracts: tuple[OwnerContractIdentity, ...]
    schema_version: str = CONFIG_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_IDENTITY_SCHEMA_VERSION:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "unsupported acceptance config identity schema version",
            )
        if not isinstance(self.full_rc_sha, str) or _GIT_SHA.fullmatch(self.full_rc_sha) is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted config full_rc_sha is invalid",
            )
        require_identifier(self.dataset_id, "config dataset_id")
        require_sha256(self.dataset_digest, "config dataset digest")
        if (
            isinstance(self.dataset_version, bool)
            or not isinstance(self.dataset_version, int)
            or self.dataset_version <= 0
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted config dataset version must be a positive integer",
            )
        require_identifier(self.dataset_timeframe, "config dataset timeframe")
        require_identifier(self.strategy_id, "config strategy_id")
        require_sha256(self.strategy_package_hash, "config strategy package hash")
        require_sha256(self.backtest_config_digest, "config Backtest configuration digest")
        require_sha256(self.backtest_performance_digest, "config Backtest performance digest")
        if self.valuation_policy not in _ALLOWED_VALUATION_POLICIES:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted config valuation policy is unsupported",
            )
        if self.funding_identity_digest is not None:
            require_sha256(self.funding_identity_digest, "config funding identity digest")
        if self.mark_identity_digest is not None:
            require_sha256(self.mark_identity_digest, "config mark identity digest")
        if self.valuation_policy == "mark" and self.mark_identity_digest is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "mark valuation requires a registered mark identity digest",
            )
        if self.valuation_policy == "last" and self.mark_identity_digest is not None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "last valuation must not publish a mark identity digest",
            )
        require_sha256(
            self.trading_runner_config_digest,
            "config Trading Runner configuration digest",
        )
        require_identifier(self.trading_runner_runtime_id, "config Trading Runner runtime_id")
        require_identifier(self.trading_runner_portfolio_id, "config Trading Runner portfolio_id")
        require_identifier(self.research_experiment_id, "config Research experiment_id")
        require_sha256(
            self.research_specification_fingerprint,
            "config Research specification fingerprint",
        )
        contracts = tuple(self.owner_contracts)
        if any(not isinstance(item, OwnerContractIdentity) for item in contracts):
            raise TypeError("owner_contracts must contain OwnerContractIdentity values")
        if contracts != tuple(sorted(contracts)):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "owner contracts must be canonically ordered",
            )
        names = tuple(item.owner for item in contracts)
        if len(names) != len(set(names)):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "owner contracts must not contain duplicate owners",
            )
        if set(names) != REQUIRED_OWNER_CONTRACTS:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "owner contracts must match the required authoritative owner set",
            )
        if contracts != STAGE1_OWNER_CONTRACTS_V1:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "owner contract versions must match Stage-1 V1",
            )
        object.__setattr__(self, "owner_contracts", contracts)

    def canonical_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "full_rc_sha": self.full_rc_sha,
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
                "backtest": {
                    "config_digest": self.backtest_config_digest,
                    "performance_digest": self.backtest_performance_digest,
                    "valuation_policy": self.valuation_policy,
                    "funding_identity_digest": self.funding_identity_digest,
                    "mark_identity_digest": self.mark_identity_digest,
                },
                "trading_runner": {
                    "config_digest": self.trading_runner_config_digest,
                    "runtime_id": self.trading_runner_runtime_id,
                    "portfolio_id": self.trading_runner_portfolio_id,
                    "strategy_id": self.strategy_id,
                    "execution_mode": "paper",
                },
                "research": {
                    "experiment_id": self.research_experiment_id,
                    "specification_fingerprint": self.research_specification_fingerprint,
                },
                "owner_contracts": {item.owner: item.version for item in self.owner_contracts},
            }
        )

    @property
    def config_digest(self) -> str:
        encoded = json.dumps(
            dict(self.canonical_mapping()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def trusted_identity(self) -> TrustedAcceptanceIdentity:
        return TrustedAcceptanceIdentity(
            full_rc_sha=self.full_rc_sha,
            dataset_id=self.dataset_id,
            dataset_digest=self.dataset_digest,
            strategy_id=self.strategy_id,
            strategy_digest=self.strategy_package_hash,
            config_digest=self.config_digest,
        )


__all__ = [
    "CONFIG_IDENTITY_SCHEMA_VERSION",
    "REQUIRED_OWNER_CONTRACTS",
    "STAGE1_OWNER_CONTRACTS_V1",
    "AcceptanceConfigurationMaterial",
    "OwnerContractIdentity",
]
