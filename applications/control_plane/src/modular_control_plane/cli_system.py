from __future__ import annotations

import argparse
import os

from .client import ClientSettings, ControlClient
from .observability import ObservabilityHealth, UnifiedObservabilitySnapshot
from .observability_client import ObservabilityControlClient
from .system_artifact_capacity import ExternalArtifactCapacitySnapshot
from .system_capacity import SystemCapacitySnapshot
from .system_capacity_client import SystemCapacityControlClient
from .system_resources import SystemResourcesSnapshot

_OBSERVABILITY_EXIT = {
    ObservabilityHealth.HEALTHY: 0,
    ObservabilityHealth.DEGRADED: 2,
    ObservabilityHealth.BLOCKED: 3,
    ObservabilityHealth.UNAVAILABLE: 3,
}


def add_system_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    system = commands.add_parser("system", help="Inspect local Control Plane system resources.")
    system_commands = system.add_subparsers(dest="system_command", required=True)
    resources = system_commands.add_parser(
        "resources",
        help="Show bounded CPU, memory, disk and Control Plane process metrics.",
    )
    resources.add_argument("--json", action="store_true", help="Emit the versioned JSON contract.")
    observability = system_commands.add_parser(
        "observability",
        help="Show fail-closed unified PAPER component health and active operator alerts.",
    )
    observability.add_argument(
        "--json", action="store_true", help="Emit the versioned JSON contract."
    )
    capacity = system_commands.add_parser(
        "capacity",
        help="Show fail-closed owner-backed storage capacity policy projection.",
    )
    capacity.add_argument("--json", action="store_true", help="Emit the versioned JSON contract.")
    artifact_capacity = system_commands.add_parser(
        "artifact-capacity",
        help="Show external GitHub Actions artifact quota status without cleanup authority.",
    )
    artifact_capacity.add_argument(
        "--json", action="store_true", help="Emit the versioned JSON contract."
    )


def run_system_command(client: ControlClient, args: argparse.Namespace) -> int:
    if args.system_command == "resources":
        resources = client.system_resources()
        _print_system_resources(resources, as_json=args.json)
        return 0
    if args.system_command == "observability":
        snapshot = _observability_client(args).observability()
        _print_observability(snapshot, as_json=args.json)
        return _OBSERVABILITY_EXIT[snapshot.overview.overall_health]
    if args.system_command == "capacity":
        system_capacity = _capacity_client(args).system_capacity()
        _print_system_capacity(system_capacity, as_json=args.json)
        return 0
    if args.system_command == "artifact-capacity":
        artifact_capacity = _capacity_client(args).external_artifact_capacity()
        _print_external_artifact_capacity(artifact_capacity, as_json=args.json)
        return 0
    raise ValueError(f"unsupported system command: {args.system_command}")


def _observability_client(args: argparse.Namespace) -> ObservabilityControlClient:
    token = os.environ.get("MODULAR_CONTROL_READER_TOKEN")
    return ObservabilityControlClient(
        ClientSettings(
            base_url=args.url,
            reader_token=token if token else None,
            timeout_seconds=args.timeout,
        )
    )


def _capacity_client(args: argparse.Namespace) -> SystemCapacityControlClient:
    token = os.environ.get("MODULAR_CONTROL_READER_TOKEN")
    return SystemCapacityControlClient(
        ClientSettings(
            base_url=args.url,
            reader_token=token if token else None,
            timeout_seconds=args.timeout,
        )
    )


def _print_system_resources(resources: SystemResourcesSnapshot, *, as_json: bool) -> None:
    if as_json:
        print(resources.model_dump_json(indent=2))
        return
    print(f"COLLECTED  {resources.collected_at.isoformat()}")
    print(f"CPU        {resources.cpu_percent:.1f}% logical={resources.logical_cpu_count or '-'}")
    print(
        f"MEMORY     {resources.memory.percent:.1f}% "
        f"used={resources.memory.used_bytes} available={resources.memory.available_bytes} "
        f"total={resources.memory.total_bytes}"
    )
    print(
        f"DISK       {resources.disk.percent:.1f}% "
        f"used={resources.disk.used_bytes} available={resources.disk.available_bytes} "
        f"total={resources.disk.total_bytes}"
    )
    print(f"UPTIME     {resources.uptime_seconds:.1f}s")
    print(f"PROCESSES  {resources.process_count}")
    print(
        f"CONTROL    rss={resources.control_process_rss_bytes} "
        f"threads={resources.control_process_threads}"
    )


def _print_observability(snapshot: UnifiedObservabilitySnapshot, *, as_json: bool) -> None:
    if as_json:
        print(snapshot.model_dump_json(indent=2))
        return
    overview = snapshot.overview
    print(f"STATUS     {overview.overall_health.value.upper()}")
    print(f"EVALUATED  {overview.evaluated_at.isoformat()}")
    print(f"DIGEST     {overview.overview_hash}")
    for component in overview.components:
        flags = []
        if component.stale:
            flags.append("stale")
        if component.stuck:
            flags.append("stuck")
        age = "UNKNOWN" if component.age_seconds is None else f"{component.age_seconds:.1f}s"
        suffix = f" flags={','.join(flags)}" if flags else ""
        print(
            f"COMPONENT  {component.component.value} "
            f"health={component.health.value.upper()} age={age}{suffix}"
        )
        if component.reason_codes:
            print(f"  REASONS  {','.join(component.reason_codes)}")
    if not snapshot.active_alerts:
        print("ALERTS     none")
        return
    print(f"ALERTS     {len(snapshot.active_alerts)}")
    for alert in snapshot.active_alerts:
        print(
            f"ALERT      {alert.severity.value.upper()} component={alert.component.value} "
            f"reasons={','.join(alert.reason_codes)}"
        )
        print(f"  ACTION   {alert.next_action}")


def _print_system_capacity(capacity: SystemCapacitySnapshot, *, as_json: bool) -> None:
    if as_json:
        print(capacity.model_dump_json(indent=2))
        return
    print(f"OBSERVED   {capacity.observed_at.isoformat()}")
    print(f"POLICY     {capacity.policy_status} source_schema={capacity.source_schema_version}")
    print(f"DIGEST     {capacity.source_digest_sha256}")
    print(f"UNKNOWN    {capacity.unknown_means}")
    print("LOCAL      global physical watermark unavailable")
    for watermark in capacity.runner_watermarks:
        print(
            f"RUNNER     {watermark.role.value} "
            f"high_free={watermark.high_water_free_bytes} "
            f"critical_free={watermark.critical_free_bytes}"
        )
    for tier in capacity.tiers:
        high = tier.physical_free_disk.high_water_free_bytes
        critical = tier.physical_free_disk.critical_free_bytes
        print(
            f"TIER       {tier.tier_id.value} status={tier.status} "
            f"physical={tier.physical_free_disk.status} "
            f"high_free={high if high is not None else 'UNKNOWN'} "
            f"critical_free={critical if critical is not None else 'UNKNOWN'} "
            "deletion=NONE"
        )
        if tier.next_publication_admission:
            print(
                "  ADMISSION next_publication=true "
                f"recovery_reserve={tier.recovery_reserve_bytes or 0}"
            )
        if tier.owner_hard_limit_bytes is not None:
            print(f"  HARD     owner_limit={tier.owner_hard_limit_bytes}")
        if tier.owner_payload_limit_bytes is not None:
            print(f"  PAYLOAD  owner_limit={tier.owner_payload_limit_bytes}")
        for measurement in tier.measurements:
            print(
                f"  MEASURE  {measurement.metric}={measurement.value:g} "
                f"unit={measurement.unit.value} aggregatable=false"
            )


def _print_external_artifact_capacity(
    capacity: ExternalArtifactCapacitySnapshot,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(capacity.model_dump_json(indent=2))
        return
    print(f"OBSERVED   {capacity.observed_at.isoformat()}")
    print(f"STATUS     {capacity.status}")
    print(f"USAGE      {capacity.proactive_usage_status}")
    print(f"CRITICAL   {capacity.critical_signal}")
    print("LOCAL      physical disk watermark not applicable")
    print(
        f"MAINT      max_count={capacity.maintenance_max_count} "
        f"max_bytes={capacity.maintenance_max_bytes} "
        f"rate_limit_reserve={capacity.rate_limit_reserve_requests}"
    )
    print("CLEANUP    unavailable in Control UI")
