from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import psutil

from ..service import utc_now
from ..system_resources import ResourceUsage, SystemResourcesSnapshot

Clock = Callable[[], datetime]


class PsutilSystemResourcesProvider:
    """Collect bounded host aggregates without exposing host or process identities."""

    def __init__(
        self,
        *,
        sample_interval_seconds: float = 0.05,
        clock: Clock = utc_now,
    ) -> None:
        if not 0.01 <= sample_interval_seconds <= 1.0:
            raise ValueError("sample_interval_seconds must be between 0.01 and 1.0")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._sample_interval_seconds = sample_interval_seconds
        self._clock = clock

    def snapshot(self) -> SystemResourcesSnapshot:
        collected_at = self._clock()
        if not isinstance(collected_at, datetime):
            raise TypeError("clock must return datetime")
        if collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")

        cpu_percent = float(psutil.cpu_percent(interval=self._sample_interval_seconds))
        logical_cpu_count = psutil.cpu_count(logical=True)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot_time = float(psutil.boot_time())
        uptime_seconds = collected_at.timestamp() - boot_time
        if uptime_seconds < 0:
            raise RuntimeError("system boot time is inconsistent")

        process = psutil.Process()
        process_memory = process.memory_info()
        process_count = len(psutil.pids())
        if process_count < 1:
            raise RuntimeError("system process count is inconsistent")

        return SystemResourcesSnapshot(
            collected_at=collected_at,
            cpu_percent=cpu_percent,
            logical_cpu_count=logical_cpu_count,
            memory=ResourceUsage(
                total_bytes=int(memory.total),
                used_bytes=int(memory.used),
                available_bytes=int(memory.available),
                percent=float(memory.percent),
            ),
            disk=ResourceUsage(
                total_bytes=int(disk.total),
                used_bytes=int(disk.used),
                available_bytes=int(disk.free),
                percent=float(disk.percent),
            ),
            uptime_seconds=uptime_seconds,
            process_count=process_count,
            control_process_rss_bytes=int(process_memory.rss),
            control_process_threads=int(process.num_threads()),
        )

    def __repr__(self) -> str:
        return (
            "PsutilSystemResourcesProvider("
            f"sample_interval_seconds={self._sample_interval_seconds!r})"
        )
