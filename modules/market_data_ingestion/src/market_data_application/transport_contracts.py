from __future__ import annotations

from typing import Literal

from pydantic import Field

from .contracts import ContractModel


class VersionResponse(ContractModel):
    component: Literal["market_data_application"] = "market_data_application"
    api_version: Literal["v1"] = "v1"
    build_version: str = Field(min_length=1, max_length=64)
