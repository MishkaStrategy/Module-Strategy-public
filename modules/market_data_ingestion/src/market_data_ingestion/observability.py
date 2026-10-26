from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(
                {key: value for key, value in context.items() if "secret" not in key.lower()}
            )
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("market_data_ingestion")
    logger.setLevel(level)
    for existing in tuple(logger.handlers):
        if getattr(existing, "_market_data_ingestion_owned", False):
            logger.removeHandler(existing)
            existing.close()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.__dict__["_market_data_ingestion_owned"] = True
    logger.addHandler(handler)
    logger.propagate = False
    return logger
