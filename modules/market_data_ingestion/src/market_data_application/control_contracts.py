from __future__ import annotations

from typing import Literal

from .contracts import ContractModel, JobDetails


class JobCancellationResponse(ContractModel):
    status: Literal["cancelled", "requested", "already_terminal"]
    job: JobDetails
    backend_run_id: str | None
