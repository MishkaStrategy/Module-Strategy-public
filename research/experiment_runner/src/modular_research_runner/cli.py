from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .canonical import canonical_value
from .identity import ExperimentIdentityInputs
from .planning import ResearchPlanBuilder
from .repository import SqliteExperimentRepository
from .specification import load_research_specification


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modular-research-runner")
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="validate a research YAML/JSON file")
    validate.add_argument("specification", type=Path)

    plan = subcommands.add_parser("plan", help="render a deterministic plan summary")
    plan.add_argument("specification", type=Path)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--engine-version", required=True)
    plan.add_argument("--metric-schema-version", required=True)
    plan.add_argument("--module-api-version", required=True)

    status = subcommands.add_parser("status", help="read durable experiment status")
    status.add_argument("database", type=Path)
    status.add_argument("experiment_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        specification = load_research_specification(args.specification)
        print(
            json.dumps(
                {
                    "research_id": specification.research_id,
                    "specification_sha256": specification.fingerprint,
                    "valid": True,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "plan":
        specification = load_research_specification(args.specification)
        experiment_id = ExperimentIdentityInputs(
            specification,
            args.engine_version,
            args.metric_schema_version,
            args.module_api_version,
        ).experiment_id()
        plan = ResearchPlanBuilder(args.output_root).build(
            experiment_id=experiment_id,
            specification=specification,
        )
        print(
            json.dumps(
                canonical_value(
                    {
                        "experiment_id": experiment_id,
                        "plan_sha256": plan.fingerprint,
                        "configuration_count": len(plan.configurations),
                        "scenario_count": len(plan.scenarios),
                        "stage_count": len(plan.stages),
                        "optimization_stage_count": len(plan.optimization_stages),
                        "initial_stage_roles": sorted({job.stage_role for job in plan.jobs}),
                        "ordinary_run_count": len(plan.jobs),
                        "final_stage_count": len(plan.final_stages),
                    }
                ),
                sort_keys=True,
            )
        )
        return 0
    repository = SqliteExperimentRepository(args.database)
    print(
        json.dumps(
            {
                "experiment_id": args.experiment_id,
                "status": repository.experiment_status(args.experiment_id).value,
                "counts": {
                    status.value: count
                    for status, count in repository.counts(args.experiment_id).items()
                },
            },
            sort_keys=True,
        )
    )
    return 0
