from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from strategy_factory import stage_handlers
from strategy_factory.contracts import FactoryMode, FactoryStage
from strategy_factory.errors import CompatibilityError, ContractError
from strategy_factory.stage_handlers import (
    ArchitectureGenerationHandler,
    OptimizationPromotionHandler,
    _family_architecture_limits,
    _family_balanced_plan_allocations,
)


class _Context:
    def __init__(self, *, request, artifacts):
        self.request = request
        self._artifacts = artifacts
        self.checkpoint = SimpleNamespace(
            run_id="sf-run-ms5a-family-balanced",
            updated_at=datetime(2026, 9, 24, tzinfo=UTC),
        )
        self.manifest = SimpleNamespace(manifest_id="manifest-ms5a")
        self.batches = []

    def load_artifact(self, stage: str):
        return self._artifacts[stage]

    def register_batch(self, *, batch_id: str, batch_fingerprint: str) -> None:
        self.batches.append((batch_id, batch_fingerprint))


@dataclass(frozen=True)
class _Budget:
    maximum_architectures: int
    maximum_experiments: int
    maximum_finalists: int
    stage_experiment_limits: dict[str, int]


@dataclass(frozen=True)
class _Request:
    mode: FactoryMode
    allowed_strategy_families: frozenset[str]
    budget: _Budget


def _request() -> _Request:
    return _Request(
        mode=FactoryMode.CREATE,
        allowed_strategy_families=frozenset({"breakout_return", "fvg_rt", "liquidity_sweep"}),
        budget=_Budget(
            maximum_architectures=7,
            maximum_experiments=6,
            maximum_finalists=2,
            stage_experiment_limits={
                FactoryStage.COARSE_SEARCH.value: 6,
                FactoryStage.LOCAL_REFINEMENT.value: 6,
            },
        ),
    )


def test_family_allocation_helpers_preserve_equal_capacity_and_remainder() -> None:
    limits, remainder = _family_architecture_limits(
        frozenset({"breakout_return", "fvg_rt", "liquidity_sweep"}),
        7,
    )
    assert limits == {
        "breakout_return": 2,
        "fvg_rt": 2,
        "liquidity_sweep": 2,
    }
    assert remainder == 1

    allocations, rows, unused = _family_balanced_plan_allocations(
        (
            ("plan-breakout", "breakout_return", 4),
            ("plan-fvg", "fvg_rt", 1),
            ("plan-sweep", "liquidity_sweep", 4),
        ),
        7,
    )
    assert allocations["plan-breakout"].plan_capacity == 2
    assert allocations["plan-fvg"].plan_capacity == 1
    assert allocations["plan-sweep"].plan_capacity == 2
    assert tuple(row["family_id"] for row in rows) == (
        "breakout_return",
        "fvg_rt",
        "liquidity_sweep",
    )
    assert unused == 2


def test_family_balanced_architecture_generation_keeps_signal_identity(monkeypatch) -> None:
    families = ("breakout_return", "fvg_rt", "liquidity_sweep")
    hypotheses = tuple(
        SimpleNamespace(
            hypothesis_id=f"hyp-{family}",
            module_candidates=(SimpleNamespace(module_id=family),),
        )
        for family in families
    )
    monkeypatch.setattr(stage_handlers, "decode_strategy_hypothesis", lambda value: value)
    calls = []

    class Planner:
        def generate_architectures(self, *, request, hypotheses, **kwargs):
            del kwargs
            family = next(iter(request.allowed_strategy_families))
            calls.append((family, request.budget.maximum_architectures, len(hypotheses)))
            architecture = SimpleNamespace(
                architecture_id=f"arch-{family}",
                signal=SimpleNamespace(module_id=family),
            )
            return SimpleNamespace(
                architectures=(architecture,),
                rejections=(),
                truncated=False,
            )

    context = _Context(
        request=_request(),
        artifacts={
            FactoryStage.HYPOTHESIS_GENERATION.value: {"hypotheses": hypotheses},
        },
    )
    outcome = ArchitectureGenerationHandler(
        Planner(),
        family_balanced=True,
    ).execute(context)

    assert calls == [
        ("breakout_return", 2, 1),
        ("fvg_rt", 2, 1),
        ("liquidity_sweep", 2, 1),
    ]
    assert tuple(item["family_id"] for item in outcome.artifact["family_allocations"]) == families
    assert outcome.artifact["unused_architecture_capacity"] == 4
    assert "FAMILY_BALANCED_ARCHITECTURES" in outcome.reason_codes
    assert outcome.architectures_generated == 3


class _Research:
    def restore_plan(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            native_plan=SimpleNamespace(
                optimization_stages=(
                    SimpleNamespace(stage_index=1),
                    SimpleNamespace(stage_index=2),
                )
            )
        )


class _Execution:
    def restore_submission(self, prepared, *, expected_experiment_id):
        del prepared
        return SimpleNamespace(experiment_id=expected_experiment_id)

    def frozen_configuration_hashes(self, experiment_id):
        return (f"hash-{experiment_id}",)

    def activate_optimization_stage(
        self,
        experiment_id,
        *,
        stage_index,
        candidate_configuration_hashes,
        activated_at,
    ):
        del candidate_configuration_hashes, activated_at
        return SimpleNamespace(run_ids=(f"run-{experiment_id}-{stage_index}",))

    def resume(self, experiment_id):
        return SimpleNamespace(
            failure=None,
            snapshot=SimpleNamespace(
                status="completed",
                has_nonterminal_runs=False,
                run_ids=(f"run-{experiment_id}",),
            ),
        )


class _Selector:
    def __init__(self, candidates):
        self._candidates = candidates

    def select(
        self,
        *,
        experiment_id,
        architecture,
        maximum_candidates,
        generated_at,
        allow_empty=False,
    ):
        del architecture, maximum_candidates, generated_at, allow_empty
        candidate = self._candidates[experiment_id]
        ranked = SimpleNamespace(candidate=candidate, selection_reasons=("PARETO",))
        return SimpleNamespace(
            selected=(ranked,),
            rejected=(),
            activation_hashes=(candidate.configuration_hash,),
            baseline_control=None,
        )

    def select_across_experiments(
        self,
        *,
        experiments,
        allowed_producer_hashes,
        maximum_candidates,
        generated_at,
    ):
        del generated_at
        selected = []
        source_ids = {}
        producer_hashes = {}
        for experiment_id, architecture in experiments[:maximum_candidates]:
            candidate = self._candidates[experiment_id]
            assert candidate.architecture is architecture
            assert candidate.configuration_hash in allowed_producer_hashes[experiment_id]
            selected.append(
                SimpleNamespace(
                    candidate=candidate,
                    pareto_front=0,
                    constraint_results={"trade_count": True},
                    objective_values={"profit_factor": 1.25},
                    selection_reasons=("GLOBAL_PARETO",),
                )
            )
            source_ids[candidate.candidate_id] = experiment_id
            producer_hashes[candidate.candidate_id] = candidate.configuration_hash
        return SimpleNamespace(
            selected=tuple(selected),
            rejected=(),
            source_experiment_ids=source_ids,
            producer_configuration_hashes=producer_hashes,
        )


def test_family_balanced_coarse_and_local_promotion_freezes_global_finalists(
    monkeypatch,
) -> None:
    families = ("breakout_return", "fvg_rt", "liquidity_sweep")
    plans = []
    source_experiments = []
    candidates = {}
    for family in families:
        plan_id = f"plan-{family}"
        experiment_id = f"exp-{family}"
        architecture = SimpleNamespace(
            architecture_id=f"arch-{family}",
            signal=SimpleNamespace(module_id=family),
        )
        plan = SimpleNamespace(
            plan_id=plan_id,
            architecture=architecture,
            parameter_space=SimpleNamespace(maximum_combinations=4),
        )
        plans.append(
            {
                "factory_plan": plan,
                "split": {"fixture": True},
                "search_method": "staged",
                "experiment_count": 4,
                "strategy_family": family,
            }
        )
        source_experiments.append(
            {
                "factory_plan_id": plan_id,
                "experiment_id": experiment_id,
            }
        )
        candidates[experiment_id] = SimpleNamespace(
            candidate_id=f"candidate-{family}",
            configuration_hash=f"hash-{experiment_id}",
            architecture=architecture,
        )

    monkeypatch.setattr(stage_handlers, "decode_experiment_plan", lambda value: value)
    monkeypatch.setattr(stage_handlers, "_split_from_mapping", lambda value: value)

    request = _request()
    planning = {"plans": tuple(plans)}
    selector = _Selector(candidates)
    execution = _Execution()
    research = _Research()

    coarse_context = _Context(
        request=request,
        artifacts={
            FactoryStage.EXPERIMENT_PLANNING.value: planning,
            FactoryStage.STRUCTURAL_SCREENING.value: {"experiments": tuple(source_experiments)},
        },
    )
    coarse = OptimizationPromotionHandler(
        factory_stage=FactoryStage.COARSE_SEARCH,
        source_stage=FactoryStage.STRUCTURAL_SCREENING,
        optimization_stage_index=1,
        research_adapter=research,
        execution_adapter=execution,
        selector=selector,
        family_balanced=True,
    )
    assert coarse.budget_request(coarse_context).experiments == 6
    coarse_outcome = coarse.execute(coarse_context)

    assert (
        tuple(item["strategy_family"] for item in coarse_outcome.artifact["experiments"])
        == families
    )
    assert coarse_outcome.artifact["unused_stage_capacity"] == 0
    assert "FAMILY_BALANCED_PROMOTION_CAPACITY" in coarse_outcome.reason_codes

    local_context = _Context(
        request=request,
        artifacts={
            FactoryStage.EXPERIMENT_PLANNING.value: planning,
            FactoryStage.COARSE_SEARCH.value: coarse_outcome.artifact,
        },
    )
    local = OptimizationPromotionHandler(
        factory_stage=FactoryStage.LOCAL_REFINEMENT,
        source_stage=FactoryStage.COARSE_SEARCH,
        optimization_stage_index=2,
        research_adapter=research,
        execution_adapter=execution,
        selector=selector,
        family_balanced=True,
        freeze_finalists=True,
    )
    assert local.budget_request(local_context).experiments == 6
    local_outcome = local.execute(local_context)

    assert local_outcome.finalists_selected == 2
    assert local_outcome.artifact["finalist_selection_policy"] == ("global-pareto-finalists-v1")
    assert local_outcome.artifact["finalist_count"] == 2
    assert tuple(row["strategy_family"] for row in local_outcome.artifact["finalists"]) == (
        "breakout_return",
        "fvg_rt",
    )
    assert "GLOBAL_SEARCH_FINALISTS_FROZEN" in local_outcome.reason_codes
    assert len(local_context.batches) == 1


def test_family_balanced_architecture_contract_guards_fail_closed() -> None:
    with pytest.raises(ContractError, match="requires families"):
        _family_architecture_limits(frozenset(), 3)

    for invalid in (True, 0, -1, "3"):
        with pytest.raises(ContractError, match="positive int"):
            _family_architecture_limits(frozenset({"a"}), invalid)  # type: ignore[arg-type]

    with pytest.raises(ContractError, match="cannot allocate one architecture"):
        _family_architecture_limits(frozenset({"a", "b"}), 1)

    with pytest.raises(ContractError, match="directions cannot be empty"):
        ArchitectureGenerationHandler(SimpleNamespace(), directions=frozenset())

    with pytest.raises(ContractError, match="family_balanced must be boolean"):
        ArchitectureGenerationHandler(SimpleNamespace(), family_balanced="yes")  # type: ignore[arg-type]


def test_family_balanced_architecture_generation_rejects_missing_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_handlers, "decode_strategy_hypothesis", lambda value: value)
    hypotheses = (
        SimpleNamespace(
            hypothesis_id="hyp-breakout",
            module_candidates=(SimpleNamespace(module_id="breakout_return"),),
        ),
        SimpleNamespace(
            hypothesis_id="hyp-fvg",
            module_candidates=(SimpleNamespace(module_id="fvg_rt"),),
        ),
    )

    class Planner:
        def generate_architectures(self, *, request, **kwargs):
            del kwargs
            family = next(iter(request.allowed_strategy_families))
            return SimpleNamespace(
                architectures=(
                    SimpleNamespace(
                        architecture_id=f"arch-{family}",
                        signal=SimpleNamespace(module_id=family),
                    ),
                ),
                rejections=(),
                truncated=False,
            )

    context = _Context(
        request=_request(),
        artifacts={FactoryStage.HYPOTHESIS_GENERATION.value: {"hypotheses": hypotheses}},
    )
    with pytest.raises(CompatibilityError, match="has no generated hypothesis"):
        ArchitectureGenerationHandler(Planner(), family_balanced=True).execute(context)


@pytest.mark.parametrize("with_reason", (False, True))
def test_family_balanced_architecture_generation_rejects_empty_family_result(
    monkeypatch: pytest.MonkeyPatch,
    with_reason: bool,
) -> None:
    monkeypatch.setattr(stage_handlers, "decode_strategy_hypothesis", lambda value: value)
    families = ("breakout_return", "fvg_rt", "liquidity_sweep")
    hypotheses = tuple(
        SimpleNamespace(
            hypothesis_id=f"hyp-{family}",
            module_candidates=(SimpleNamespace(module_id=family),),
        )
        for family in families
    )

    class Planner:
        def generate_architectures(self, **kwargs):
            del kwargs
            rejections = (
                (SimpleNamespace(reason_codes=("REJECTED_FOR_TEST",)),) if with_reason else ()
            )
            return SimpleNamespace(architectures=(), rejections=rejections, truncated=False)

    context = _Context(
        request=_request(),
        artifacts={FactoryStage.HYPOTHESIS_GENERATION.value: {"hypotheses": hypotheses}},
    )
    match = "REJECTED_FOR_TEST" if with_reason else "NO_COMPATIBLE_COMBINATION"
    with pytest.raises(CompatibilityError, match=match):
        ArchitectureGenerationHandler(Planner(), family_balanced=True).execute(context)


def test_family_balanced_architecture_generation_rejects_cross_family_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_handlers, "decode_strategy_hypothesis", lambda value: value)
    families = ("breakout_return", "fvg_rt", "liquidity_sweep")
    hypotheses = tuple(
        SimpleNamespace(
            hypothesis_id=f"hyp-{family}",
            module_candidates=(SimpleNamespace(module_id=family),),
        )
        for family in families
    )

    class Planner:
        def generate_architectures(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                architectures=(
                    SimpleNamespace(
                        architecture_id="arch-wrong",
                        signal=SimpleNamespace(module_id="wrong-family"),
                    ),
                ),
                rejections=(),
                truncated=False,
            )

    context = _Context(
        request=_request(),
        artifacts={FactoryStage.HYPOTHESIS_GENERATION.value: {"hypotheses": hypotheses}},
    )
    with pytest.raises(CompatibilityError, match="crossed SIGNAL family identity"):
        ArchitectureGenerationHandler(Planner(), family_balanced=True).execute(context)


def test_generic_architecture_generation_rejects_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_handlers, "decode_strategy_hypothesis", lambda value: value)
    hypothesis = SimpleNamespace(
        hypothesis_id="hyp-breakout",
        module_candidates=(SimpleNamespace(module_id="breakout_return"),),
    )

    class Planner:
        def generate_architectures(self, **kwargs):
            del kwargs
            return SimpleNamespace(architectures=(), rejections=(), truncated=False)

    context = _Context(
        request=_request(),
        artifacts={FactoryStage.HYPOTHESIS_GENERATION.value: {"hypotheses": (hypothesis,)}},
    )
    with pytest.raises(CompatibilityError, match="NO_COMPATIBLE_COMBINATION"):
        ArchitectureGenerationHandler(Planner()).execute(context)
