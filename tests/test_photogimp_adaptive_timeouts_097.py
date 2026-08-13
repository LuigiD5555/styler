from pathlib import Path

from styler.component_catalog.compiler import compile_workflow
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve


def test_photogimp_gimp_initialization_has_no_outer_fixed_150_second_timeout() -> None:
    registry = ComponentRegistry.from_report(load(root="."))
    resolution = resolve(registry, ["app.photogimp"], family="ubuntu")
    compiled = compile_workflow(registry, resolution)
    step = next(item for item in compiled.workflow.steps if item.step_type == "initialize_flatpak_app")

    assert step.timeout is None
    assert step.config["config_creation_timeout_seconds"] == 30
    assert step.config["config_creation_max_seconds"] == 600
    assert step.config["config_flush_timeout_seconds"] == 20
    assert step.config["config_flush_max_seconds"] == 600


def test_package_installer_declares_activity_timeout_instead_of_only_total_timeout() -> None:
    source = (Path(__file__).resolve().parents[1] / "styler/runtime/executors.py").read_text(encoding="utf-8")
    assert 'step.config.get("idle_timeout_seconds", 300)' in source
    assert "idle_timeout=idle_timeout" in source
    assert 'command.timeout_reason == "idle"' in source
