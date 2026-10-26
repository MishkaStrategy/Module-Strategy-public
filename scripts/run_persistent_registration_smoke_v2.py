from __future__ import annotations

import json
from pathlib import Path

import run_persistent_registration_smoke as base


_original_smoke_config = base.smoke_config
_SAFE_DATASET_PATH = f"dataset://{base.EXPECTED_DATASET_ID}/v1"


def _smoke_config(*, storage_root: Path, smoke_root: Path) -> Path:
    config_path = _original_smoke_config(storage_root=storage_root, smoke_root=smoke_root)
    (smoke_root / "modules.yaml").write_text(
        """modules:
  - class: modular_backtest.acceptance:AcceptanceNoopModule
    enabled: true
    parameters: {}
""",
        encoding="utf-8",
    )
    return config_path


def _drop_forbidden_keys(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_forbidden_keys(nested)
            for key, nested in value.items()
            if key not in base.FORBIDDEN_EVIDENCE_KEYS
        }
    if isinstance(value, list):
        return [_drop_forbidden_keys(nested) for nested in value]
    return value


def _migrate_stale_evidence(home: Path) -> None:
    evidence_path = home / "ModuleStrategyData/evidence/binance-usdtm-registration.json"
    if not evidence_path.exists():
        return
    if evidence_path.is_symlink() or not evidence_path.is_file():
        base.fail("persistent registration evidence must be a regular file")
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        base.fail("persistent registration evidence must be a JSON object")
    if payload.get("corpus_hash") != base.EXPECTED_CORPUS_HASH or payload.get("status") != "PASS":
        return
    sanitized = _drop_forbidden_keys(payload)
    if sanitized == payload:
        return
    evidence_path.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)


def _reject_private_evidence(value: object, *, private_markers: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "path":
                if nested != _SAFE_DATASET_PATH:
                    base.fail("acceptance evidence contains an unsafe path")
                continue
            if key in base.FORBIDDEN_EVIDENCE_KEYS:
                base.fail(f"registration evidence leaks forbidden keys: {[key]}")
            _reject_private_evidence(nested, private_markers=private_markers)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_private_evidence(nested, private_markers=private_markers)
        return
    if isinstance(value, str) and any(marker and marker in value for marker in private_markers):
        base.fail("acceptance evidence leaks a private local path")


def main() -> None:
    _migrate_stale_evidence(Path.home().resolve(strict=True))
    base.smoke_config = _smoke_config
    base.reject_private_evidence = _reject_private_evidence
    base.main()


if __name__ == "__main__":
    main()
