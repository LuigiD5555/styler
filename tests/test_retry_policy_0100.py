from __future__ import annotations

from pathlib import Path

from styler.change_recipe import compile_recipe, loads_recipe
from styler.runtime.engine import WorkflowEngine
from styler.runtime.executors import ExecutorRegistry, StepExecutor
from styler.runtime.models import ExecutionContext, Status, StepDefinition, StepResult, WorkflowDefinition


def test_generic_package_install_gets_one_retry(tmp_path: Path) -> None:
    recipe_file = tmp_path / "retry.yaml"
    recipe_file.write_text(
        """schema: styler.recipe/1
recipe_id: retry-package
name: Retry package
operations:
  - id: install
    kind: package.install
    title: Instalar GIMP
    config:
      manager: flatpak
      name: org.gimp.GIMP
""",
        encoding="utf-8",
    )
    workflow = compile_recipe(loads_recipe(recipe_file.read_text(encoding="utf-8")))
    install = next(step for step in workflow.steps if step.id == "op.install")
    assert install.step_type == "install_package"
    assert install.retries == 1
    assert install.retry_delay == 2.0


def test_appimagelauncher_download_and_install_are_retryable() -> None:
    root = Path(__file__).resolve().parents[1]
    recipe_path = root / "styler/catalog/changes/appimagelauncher.yaml"
    recipe = loads_recipe(recipe_path.read_text(encoding="utf-8"))
    workflow = compile_recipe(recipe)
    by_id = {step.id: step for step in workflow.steps}
    assert by_id["op.download"].step_type == "fetch_release_artifact"
    assert by_id["op.download"].retries == 1
    assert by_id["op.install"].step_type == "install_package_artifact"
    assert by_id["op.install"].retries == 1


class _EffectThenFailExecutor(StepExecutor):
    def __init__(self) -> None:
        self.run_calls = 0
        self.effect_present = False

    @property
    def step_type(self) -> str:
        return "effect_then_fail"

    def reconcile(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult | None:
        if not self.effect_present:
            return None
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.RECONCILED,
            "El efecto del primer intento ya está presente.",
        )

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        self.run_calls += 1
        self.effect_present = True
        return StepResult.failed(step, "falló después de producir el efecto", "AFTER_EFFECT")


def test_retry_reconciles_before_repeating_side_effect(tmp_path: Path) -> None:
    executor = _EffectThenFailExecutor()
    registry = ExecutorRegistry()
    registry.register(executor)
    engine = WorkflowEngine(registry)
    step = StepDefinition("install", "effect_then_fail", retries=1)
    workflow = WorkflowDefinition("retry-safe", [step])
    ctx = ExecutionContext(root=tmp_path, run_id="retry-safe", approve=True)

    run = engine.run(workflow, ctx)

    assert run.success
    assert executor.run_calls == 1
    assert run.results[0].status == Status.RECONCILED
    assert run.results[0].attempts == 2
    assert run.results[0].data["retry_reconciled"] is True
