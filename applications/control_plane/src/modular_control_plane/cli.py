from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from .acceptance import (
    AcceptanceCurrent,
    AcceptanceMutationResult,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
)
from .backtest import BacktestJobRecord, BacktestResultSummary
from .cli_dataset import add_dataset_commands, run_dataset_command
from .cli_system import add_system_commands, run_system_command
from .client import ClientSettings, ControlClient, ControlClientError
from .contracts import ComponentHealth, ComponentId, HealthStatus, SystemOverview
from .evidence import AuditDecisionList, AuditEvidenceList, EvidenceOverview, ResultsEvidenceList
from .paper import PaperBlockedScopeList, PaperOrderList, PaperRuntimeSummary
from .research import (
    ResearchExperimentList,
    ResearchExperimentSummary,
    ResearchRunList,
    ResearchRunSummary,
)
from .strategy import StrategyRunList, StrategyRunSummary

_EXIT_BY_HEALTH = {
    HealthStatus.HEALTHY: 0,
    HealthStatus.DEGRADED: 2,
    HealthStatus.UNAVAILABLE: 3,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        client = ControlClient(
            ClientSettings(
                base_url=args.url,
                reader_token=_optional_secret(os.environ.get("MODULAR_CONTROL_READER_TOKEN")),
                operator_token=_optional_secret(os.environ.get("MODULAR_CONTROL_OPERATOR_TOKEN")),
                timeout_seconds=args.timeout,
            )
        )
        if args.command == "status":
            overview = client.status()
            _print_overview(overview, as_json=args.json)
            return _EXIT_BY_HEALTH[overview.status]
        if args.command == "health":
            if args.component is None:
                overview = client.status()
                _print_overview(overview, as_json=args.json)
                return _EXIT_BY_HEALTH[overview.status]
            component = ComponentId(args.component)
            health = client.component_health(component)
            _print_component(health, as_json=args.json)
            return _EXIT_BY_HEALTH[health.status]
        if args.command == "dataset":
            return run_dataset_command(client, args)
        if args.command == "system":
            return run_system_command(client, args)
        if args.command == "strategy":
            return _run_strategy(client, args)
        if args.command == "backtest":
            return _run_backtest(client, args)
        if args.command == "research":
            return _run_research(client, args)
        if args.command == "paper":
            return _run_paper(client, args)
        if args.command == "acceptance":
            return _run_acceptance(client, args)
        if args.command == "evidence":
            return _run_evidence(client, args)
        parser.error(f"unsupported command: {args.command}")
    except (ControlClientError, ValueError, TypeError) as exc:
        return _print_error(exc, as_json=bool(getattr(args, "json", False)))
    return 2


def _run_strategy(client: ControlClient, args: argparse.Namespace) -> int:
    if args.strategy_command == "list":
        runs = client.strategy_runs(limit=args.limit, offset=args.offset)
        _print_strategy_runs(runs, as_json=args.json)
        return 0
    if args.strategy_command == "show":
        run = client.strategy_run(args.run_id)
        _print_strategy_run(run, as_json=args.json)
        return 0
    raise ValueError(f"unsupported strategy command: {args.strategy_command}")


def _run_backtest(client: ControlClient, args: argparse.Namespace) -> int:
    if args.backtest_command == "list":
        records = client.backtests(limit=args.limit, offset=args.offset)
        _print_backtests(records, as_json=args.json)
        return 0
    if args.backtest_command == "show":
        record = client.backtest(args.job_id)
        _print_backtest(record, as_json=args.json)
        return 0
    if args.backtest_command == "result":
        result = client.backtest_result(args.job_id)
        _print_backtest_result(result, as_json=args.json)
        return 0
    raise ValueError(f"unsupported backtest command: {args.backtest_command}")


def _run_research(client: ControlClient, args: argparse.Namespace) -> int:
    if args.research_command == "experiments":
        experiments = client.research_experiments(limit=args.limit, offset=args.offset)
        _print_research_experiments(experiments, as_json=args.json)
        return 0
    if args.research_command == "experiment":
        experiment = client.research_experiment(args.experiment_id)
        _print_research_experiment(experiment, as_json=args.json)
        return 0
    if args.research_command == "runs":
        runs = client.research_runs(
            args.experiment_id,
            limit=args.limit,
            offset=args.offset,
        )
        _print_research_runs(runs, as_json=args.json)
        return 0
    if args.research_command == "run":
        run = client.research_run(args.run_id)
        _print_research_run(run, as_json=args.json)
        return 0
    raise ValueError(f"unsupported research command: {args.research_command}")


def _run_paper(client: ControlClient, args: argparse.Namespace) -> int:
    if args.paper_command == "status":
        runtime = client.paper_runtime()
        _print_paper_runtime(runtime, as_json=args.json)
        return 0
    if args.paper_command == "orders":
        orders = client.paper_orders(limit=args.limit, offset=args.offset)
        _print_paper_orders(orders, as_json=args.json)
        return 0
    if args.paper_command == "blocked-scopes":
        scopes = client.paper_blocked_scopes()
        _print_paper_blocked_scopes(scopes, as_json=args.json)
        return 0
    raise ValueError(f"unsupported paper command: {args.paper_command}")


def _run_acceptance(client: ControlClient, args: argparse.Namespace) -> int:
    command = args.acceptance_command
    if command == "status":
        current = client.acceptance_current()
        _print_acceptance_current(current, as_json=args.json)
        return 0
    if command == "start":
        result = client.acceptance_start(
            AcceptanceStartRequest(
                request_id=args.request_id,
                dataset_id=args.dataset_id,
                dataset_digest=args.dataset_digest,
                strategy_id=args.strategy_id,
                strategy_digest=args.strategy_digest,
                config_digest=args.config_digest,
                enable_soak_24h=args.soak_24h,
                enable_soak_72h=args.soak_72h,
            )
        )
        _print_acceptance_mutation(result, as_json=args.json)
        return 0
    request = AcceptanceRunMutationRequest(
        request_id=args.request_id,
        acceptance_run_id=args.run_id,
    )
    if command == "stop":
        result = client.acceptance_stop(request)
    elif command == "cancel":
        result = client.acceptance_cancel(request)
    elif command == "retry":
        result = client.acceptance_retry(request)
    else:
        raise ValueError(f"unsupported acceptance command: {command}")
    _print_acceptance_mutation(result, as_json=args.json)
    return 0


def _run_evidence(client: ControlClient, args: argparse.Namespace) -> int:
    if args.evidence_command == "overview":
        overview = client.evidence_overview()
        _print_evidence_overview(overview, as_json=args.json)
        return 0
    if args.evidence_command == "audit-decisions":
        decisions = client.audit_decisions(limit=args.limit, offset=args.offset)
        _print_audit_decisions(decisions, as_json=args.json)
        return 0
    if args.evidence_command == "audit-evidence":
        audit_items = client.audit_evidence(limit=args.limit, offset=args.offset)
        _print_audit_evidence(audit_items, as_json=args.json)
        return 0
    if args.evidence_command == "results":
        result_items = client.results_evidence(limit=args.limit, offset=args.offset)
        _print_results_evidence(result_items, as_json=args.json)
        return 0
    raise ValueError(f"unsupported evidence command: {args.evidence_command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modular-control")
    parser.add_argument(
        "--url",
        default=os.environ.get("MODULAR_CONTROL_URL", "http://127.0.0.1:8765"),
        help="Loopback Control Plane base URL.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("MODULAR_CONTROL_TIMEOUT_SECONDS", "2.0")),
        help="Monitoring request timeout in seconds (0.05..30).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="Show the whole Modular Strategy status.")
    _add_json_flag(status)

    health = commands.add_parser("health", help="Show system or component health.")
    health.add_argument(
        "component",
        nargs="?",
        choices=[component.value for component in ComponentId],
    )
    _add_json_flag(health)

    add_dataset_commands(commands)
    add_system_commands(commands)

    strategy = commands.add_parser("strategy", help="Inspect Strategy Factory governance state.")
    strategy_commands = strategy.add_subparsers(dest="strategy_command", required=True)
    strategy_list = strategy_commands.add_parser("list", help="List bounded Strategy Factory runs.")
    _add_pagination(strategy_list)
    _add_json_flag(strategy_list)
    strategy_show = strategy_commands.add_parser("show", help="Show one Strategy Factory run.")
    strategy_show.add_argument("run_id")
    _add_json_flag(strategy_show)

    backtest = commands.add_parser("backtest", help="Inspect durable Backtest Control jobs.")
    backtest_commands = backtest.add_subparsers(dest="backtest_command", required=True)
    backtest_list = backtest_commands.add_parser("list", help="List durable Backtest jobs.")
    _add_pagination(backtest_list)
    _add_json_flag(backtest_list)
    backtest_show = backtest_commands.add_parser("show", help="Show one durable Backtest job.")
    backtest_show.add_argument("job_id")
    _add_json_flag(backtest_show)
    backtest_result = backtest_commands.add_parser(
        "result", help="Show one verified Backtest result."
    )
    backtest_result.add_argument("job_id")
    _add_json_flag(backtest_result)

    research = commands.add_parser("research", help="Inspect Research Runner durable state.")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    experiments = research_commands.add_parser("experiments", help="List Research experiments.")
    _add_pagination(experiments)
    _add_json_flag(experiments)
    experiment = research_commands.add_parser("experiment", help="Show one Research experiment.")
    experiment.add_argument("experiment_id")
    _add_json_flag(experiment)
    research_runs = research_commands.add_parser("runs", help="List runs for one experiment.")
    research_runs.add_argument("experiment_id")
    _add_pagination(research_runs, default_limit=100)
    _add_json_flag(research_runs)
    research_run = research_commands.add_parser("run", help="Show one Research run.")
    research_run.add_argument("run_id")
    _add_json_flag(research_run)

    paper = commands.add_parser("paper", help="Inspect attached PAPER runtime state.")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    paper_status = paper_commands.add_parser("status", help="Show the attached PAPER runtime.")
    _add_json_flag(paper_status)
    paper_orders = paper_commands.add_parser("orders", help="List factual paper order projections.")
    _add_pagination(paper_orders)
    _add_json_flag(paper_orders)
    paper_blocked = paper_commands.add_parser(
        "blocked-scopes", help="List reconciliation-blocked paper scopes."
    )
    _add_json_flag(paper_blocked)

    acceptance = commands.add_parser(
        "acceptance",
        help="Inspect or mutate the durable PAPER System Acceptance owner.",
    )
    acceptance_commands = acceptance.add_subparsers(dest="acceptance_command", required=True)
    acceptance_status = acceptance_commands.add_parser(
        "status", help="Show current Acceptance run."
    )
    _add_json_flag(acceptance_status)
    acceptance_start = acceptance_commands.add_parser(
        "start",
        help="Start a candidate-pinned PAPER Acceptance run.",
    )
    acceptance_start.add_argument("--request-id", required=True)
    acceptance_start.add_argument("--dataset-id", required=True)
    acceptance_start.add_argument("--dataset-digest", required=True)
    acceptance_start.add_argument("--strategy-id", required=True)
    acceptance_start.add_argument("--strategy-digest", required=True)
    acceptance_start.add_argument("--config-digest", required=True)
    acceptance_start.add_argument("--soak-24h", action="store_true")
    acceptance_start.add_argument("--soak-72h", action="store_true")
    _add_json_flag(acceptance_start)
    for name, help_text in (
        ("stop", "Stop at the next safe owner checkpoint."),
        ("cancel", "Cancel at the next safe owner checkpoint."),
        ("retry", "Retry the durable blocked/failed Acceptance run."),
    ):
        command = acceptance_commands.add_parser(name, help=help_text)
        command.add_argument("run_id")
        command.add_argument("--request-id", required=True)
        _add_json_flag(command)

    evidence = commands.add_parser("evidence", help="Inspect immutable evidence projections.")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_overview = evidence_commands.add_parser("overview", help="Show evidence sources.")
    _add_json_flag(evidence_overview)
    audit_decisions = evidence_commands.add_parser(
        "audit-decisions", help="List Strategy Audit decisions."
    )
    _add_pagination(audit_decisions)
    _add_json_flag(audit_decisions)
    audit_evidence = evidence_commands.add_parser(
        "audit-evidence", help="List Strategy Audit evidence identities."
    )
    _add_pagination(audit_evidence)
    _add_json_flag(audit_evidence)
    results_evidence = evidence_commands.add_parser(
        "results", help="List Results Aggregator evidence identities."
    )
    _add_pagination(results_evidence)
    _add_json_flag(results_evidence)
    return parser


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit the versioned JSON contract.")


def _add_pagination(parser: argparse.ArgumentParser, *, default_limit: int = 50) -> None:
    parser.add_argument(
        "--limit",
        type=int,
        default=default_limit,
        help="Maximum rows to return (1..200).",
    )
    parser.add_argument("--offset", type=int, default=0, help="Zero-based row offset.")


def _print_overview(overview: SystemOverview, *, as_json: bool) -> None:
    if as_json:
        print(overview.model_dump_json(indent=2))
        return
    print(f"SYSTEM  {overview.status.value}")
    print(f"MODE    {overview.mode}")
    for component in overview.components:
        print(
            f"{component.component.value.upper():<13} "
            f"{component.status.value:<11} {component.summary}"
        )


def _print_component(health: ComponentHealth, *, as_json: bool) -> None:
    if as_json:
        print(health.model_dump_json(indent=2))
        return
    print(f"COMPONENT  {health.component.value}")
    print(f"STATUS     {health.status.value}")
    print(f"SUMMARY    {health.summary}")
    for name, value in health.details.items():
        print(f"{name.upper():<10} {value}")


def _print_strategy_runs(runs: StrategyRunList, *, as_json: bool) -> None:
    if as_json:
        print(runs.model_dump_json(indent=2))
        return
    print(
        f"STRATEGY RUNS  visible={len(runs.runs)} valid={runs.total_valid} "
        f"invalid={runs.invalid_count} ignored={runs.ignored_count}"
    )
    if not runs.runs:
        print("No Strategy Factory runs are visible.")
        return
    for run in runs.runs:
        print(
            f"{run.run_id:<28} {run.factory_status.value:<16} "
            f"{run.stage.value:<24} seq={run.stage_sequence}"
        )


def _print_strategy_run(run: StrategyRunSummary, *, as_json: bool) -> None:
    if as_json:
        print(run.model_dump_json(indent=2))
        return
    print(f"RUN       {run.run_id}")
    print(f"STATUS    {run.factory_status.value} / {run.status.value}")
    print(f"STAGE     {run.stage.value} ({run.stage_sequence})")
    print(f"UPDATED   {run.updated_at.isoformat()}")
    print(f"MANIFEST  {run.manifest_id}")
    print(f"COMMIT    {run.commit_sha}")
    print(f"DECISIONS {run.decision_count} verified={run.decision_chain_verified}")


def _print_backtests(records: tuple[BacktestJobRecord, ...], *, as_json: bool) -> None:
    if as_json:
        payload = [record.model_dump(mode="json") for record in records]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"BACKTEST JOBS  visible={len(records)}")
    if not records:
        print("No durable Backtest jobs are visible.")
        return
    for record in records:
        job = record.job
        print(f"{job.job_id:<24} {job.status.value:<11} {job.phase:<18} {record.config_id}")


def _print_backtest(record: BacktestJobRecord, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    job = record.job
    print(f"JOB      {job.job_id}")
    print(f"CONFIG   {record.config_id}")
    print(f"STATUS   {job.status.value}")
    print(f"PHASE    {job.phase}")
    print(f"UPDATED  {job.updated_at.isoformat()}")
    if record.result is not None:
        print(f"RUN      {record.result.run_id}")
        print(f"BARS     {record.result.bars_processed}")
        print(f"RESTART  {record.result.restart_verified}")
    if job.error is not None:
        print(f"ERROR    {job.error.code}: {job.error.message}")
        if job.error.action:
            print(f"ACTION   {job.error.action}")


def _print_backtest_result(result: BacktestResultSummary, *, as_json: bool) -> None:
    if as_json:
        print(result.model_dump_json(indent=2))
        return
    print(f"JOB      {result.job_id}")
    print(f"RUN      {result.run_id}")
    print(f"BARS     {result.bars_processed}")
    print(f"RESTART  {result.restart_verified}")
    for name, value in result.metrics.items():
        print(f"{name.upper():<16} {value}")


def _print_research_experiments(experiments: ResearchExperimentList, *, as_json: bool) -> None:
    if as_json:
        print(experiments.model_dump_json(indent=2))
        return
    print(
        f"RESEARCH EXPERIMENTS  visible={len(experiments.experiments)} "
        f"has_more={experiments.has_more}"
    )
    if not experiments.experiments:
        print("No Research experiments are visible.")
        return
    for experiment in experiments.experiments:
        print(
            f"{experiment.experiment_id:<30} {experiment.native_status.value:<24} "
            f"updated={experiment.updated_at.isoformat()}"
        )


def _print_research_experiment(experiment: ResearchExperimentSummary, *, as_json: bool) -> None:
    if as_json:
        print(experiment.model_dump_json(indent=2))
        return
    print(f"EXPERIMENT {experiment.experiment_id}")
    print(f"STATUS     {experiment.native_status.value} / {experiment.status.value}")
    print(f"UPDATED    {experiment.updated_at.isoformat()}")
    print(f"FAILURES   {experiment.has_failures}")
    for status, count in experiment.run_counts.items():
        print(f"RUNS_{status.value:<18} {count}")


def _print_research_runs(runs: ResearchRunList, *, as_json: bool) -> None:
    if as_json:
        print(runs.model_dump_json(indent=2))
        return
    print(
        f"RESEARCH RUNS  experiment={runs.experiment_id} visible={len(runs.runs)} "
        f"has_more={runs.has_more}"
    )
    if not runs.runs:
        print("No Research runs are visible.")
        return
    for run in runs.runs:
        print(
            f"{run.run_id:<30} {run.native_status.value:<12} {run.stage_id:<20} "
            f"attempt={run.attempt_count}/{run.max_attempts}"
        )


def _print_research_run(run: ResearchRunSummary, *, as_json: bool) -> None:
    if as_json:
        print(run.model_dump_json(indent=2))
        return
    print(f"RUN        {run.run_id}")
    print(f"EXPERIMENT {run.experiment_id}")
    print(f"STATUS     {run.native_status.value} / {run.status.value}")
    print(f"STAGE      {run.stage_id} ({run.stage_role})")
    print(f"DATASET    {run.dataset_id}")
    print(f"SCENARIO   {run.scenario_id}")
    print(f"ATTEMPTS   {run.attempt_count}/{run.max_attempts}")
    print(f"UPDATED    {run.updated_at.isoformat()}")
    if run.engine_run_id is not None:
        print(f"ENGINE_RUN {run.engine_run_id}")
        print(f"BARS       {run.bars_processed}")
        print(f"RESTART    {run.restart_verified}")
    if run.failure_category is not None:
        print(f"FAILURE    {run.failure_category.value} retryable={run.failure_retryable}")


def _print_paper_runtime(runtime: PaperRuntimeSummary, *, as_json: bool) -> None:
    if as_json:
        print(runtime.model_dump_json(indent=2))
        return
    print("MODE       PAPER")
    print(f"RUNTIME    {runtime.runtime_id}")
    print(f"STATE      {runtime.native_state.value}")
    print(f"EXECUTION  {runtime.paper_execution_lifecycle.value}")
    print(f"STRATEGY   {runtime.strategy_id}")
    print(f"HEALTH     {'READY' if runtime.health_ready else 'NOT_READY'}")
    print(
        f"KILL       {'ACTIVE' if runtime.kill_switch.active else 'INACTIVE'} "
        f"scope={runtime.kill_switch.scope.value}"
    )
    print(f"ORDERS     {runtime.active_order_count} active / {runtime.order_count} total")
    print(f"BLOCKED    {runtime.blocked_scope_count}")
    if runtime.queue_depth is not None and runtime.queue_capacity is not None:
        print(f"QUEUE      {runtime.queue_depth}/{runtime.queue_capacity}")
    print(f"MUTATIONS  {'AVAILABLE' if runtime.lifecycle_mutations_available else 'UNAVAILABLE'}")


def _print_paper_orders(orders: PaperOrderList, *, as_json: bool) -> None:
    if as_json:
        print(orders.model_dump_json(indent=2))
        return
    print(f"PAPER ORDERS  visible={len(orders.orders)} has_more={orders.has_more}")
    if not orders.orders:
        print("No paper orders are visible.")
        return
    for order in orders.orders:
        print(
            f"{order.client_order_id:<28} {order.instrument:<12} {order.native_state:<14} "
            f"filled={order.filled_quantity}/{order.requested_quantity}"
        )


def _print_paper_blocked_scopes(scopes: PaperBlockedScopeList, *, as_json: bool) -> None:
    if as_json:
        print(scopes.model_dump_json(indent=2))
        return
    print(f"BLOCKED SCOPES  visible={len(scopes.scopes)}")
    if not scopes.scopes:
        print("No reconciliation-blocked scopes are visible.")
        return
    for scope in scopes.scopes:
        print(f"{scope.portfolio_id}  {scope.instrument}")


def _print_acceptance_current(current: AcceptanceCurrent, *, as_json: bool) -> None:
    if as_json:
        print(current.model_dump_json(indent=2))
        return
    run = current.run
    if run is None:
        print("ACCEPTANCE  no durable run")
        return
    print(f"RUN       {run.acceptance_run_id}")
    print(f"STATUS    {run.status.value}")
    print(f"PHASE     {run.phase.value}")
    print(f"FULL_RC   {run.identity.full_rc_sha}")
    print(f"DATASET   {run.identity.dataset_id} {run.identity.dataset_digest}")
    print(f"STRATEGY  {run.identity.strategy_id} {run.identity.strategy_digest}")
    print(f"ACTIVE    {run.active_test_time:.3f}s")
    print(f"WALL      {run.wall_clock_elapsed:.3f}s")
    print(f"RESTARTS  {run.restart_count}")
    print(f"UPDATED   {run.updated_at.isoformat()}")
    if run.next_action is not None:
        print(f"NEXT      {run.next_action}")
    if run.error is not None:
        print(f"ERROR     {run.error}")


def _print_acceptance_mutation(result: AcceptanceMutationResult, *, as_json: bool) -> None:
    if as_json:
        print(result.model_dump_json(indent=2))
        return
    print(f"REQUEST   {result.request_id}")
    print(f"RUN       {result.acceptance_run_id}")
    print(f"MUTATION  {result.mutation.value}")
    print(f"STATUS    {result.status.value}")
    print(f"PHASE     {result.phase.value}")
    print(f"REPLAYED  {result.replayed}")
    print(f"COMPLETED {result.completed_at.isoformat()}")


def _print_evidence_overview(overview: EvidenceOverview, *, as_json: bool) -> None:
    if as_json:
        print(overview.model_dump_json(indent=2))
        return
    print(f"AUDIT      {'CONFIGURED' if overview.audit_configured else 'NOT_CONFIGURED'}")
    if overview.audit_integrity is not None:
        print(f"DECISIONS  {overview.audit_integrity.decision_count}")
        print(f"EVIDENCE   {overview.audit_integrity.evidence_count}")
        print(f"CHAIN      {overview.audit_integrity.hash_chain_verified}")
        print(f"HMAC       reverified={overview.audit_integrity.hmac_signatures_reverified}")
    print(
        "RESULTS    " + ("CONFIGURED" if overview.results_registry_configured else "NOT_CONFIGURED")
    )
    if overview.results_registry_revision is not None:
        print(f"REVISION   {overview.results_registry_revision}")
        print(f"RESULTS_EV {overview.results_evidence_count}")


def _print_audit_decisions(decisions: AuditDecisionList, *, as_json: bool) -> None:
    if as_json:
        print(decisions.model_dump_json(indent=2))
        return
    print(f"AUDIT DECISIONS  visible={len(decisions.decisions)} has_more={decisions.has_more}")
    if not decisions.decisions:
        print("No Strategy Audit decisions are visible.")
        return
    for decision in decisions.decisions:
        print(
            f"{decision.sequence:>6} {decision.candidate_id:<28} {decision.outcome.value:<20} "
            f"audit={decision.audit_id}"
        )


def _print_audit_evidence(evidence: AuditEvidenceList, *, as_json: bool) -> None:
    if as_json:
        print(evidence.model_dump_json(indent=2))
        return
    print(f"AUDIT EVIDENCE  visible={len(evidence.evidence)} has_more={evidence.has_more}")
    if not evidence.evidence:
        print("No Strategy Audit evidence identities are visible.")
        return
    for item in evidence.evidence:
        print(
            f"{item.candidate_id:<28} decisions={item.decision_count:<4} hash={item.evidence_hash}"
        )


def _print_results_evidence(evidence: ResultsEvidenceList, *, as_json: bool) -> None:
    if as_json:
        print(evidence.model_dump_json(indent=2))
        return
    print(
        f"RESULTS EVIDENCE  revision={evidence.registry_revision} "
        f"visible={len(evidence.evidence)} has_more={evidence.has_more}"
    )
    if not evidence.evidence:
        print("No Results evidence identities are visible.")
        return
    for item in evidence.evidence:
        print(
            f"{item.evidence_id:<28} {item.status.value:<12} {item.severity.value:<9} "
            f"experiment={item.experiment_id}"
        )


def _print_error(error: Exception, *, as_json: bool) -> int:
    if isinstance(error, ControlClientError):
        code = error.code
        message = error.message
        action = error.action
        status_code = error.status_code
    else:
        code = "CONTROL_CONFIGURATION_INVALID"
        message = str(error)
        action = "Check the local Control Plane URL, command arguments and timeout configuration."
        status_code = None
    payload: dict[str, object] = {
        "code": code,
        "message": message,
        "action": action,
        "status_code": status_code,
    }
    if as_json:
        print(json.dumps({"error": payload}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR {code}: {message}", file=sys.stderr)
        if action:
            print(f"ACTION: {action}", file=sys.stderr)
    return 4


def _optional_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
