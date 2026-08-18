"""Los .stylerpkg de cambio convergen en la misma experiencia que PhotoGIMP."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("local_execution_backend")

import hashlib
import json
from pathlib import Path

from styler.change_recipe import ChangeRecipe, RecipeOperation, compile_recipe, dumps_recipe
from styler.changes import ChangeService, ChangeStatus
from styler.portable import (
    ArtifactEntry,
    GraphDefinition,
    PackageManifest,
    PackageType,
    PortableLibrary,
    build_package,
)
from styler.portable.workflow import workflow_to_portable_dict


def _portable_change(tmp_path: Path, *, package_id: str = "demo-package") -> tuple[Path, str, GraphDefinition]:
    payload = b"Styler portable DAG\n"
    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
    recipe = ChangeRecipe(
        recipe_id="demo-change",
        name="Demo portable",
        description="Cambio portable de prueba.",
        operations=(
            RecipeOperation(
                operation_id="copy-demo",
                kind="asset.overlay",
                title="Copiar configuración demo",
                config={
                    "source": "package://assets/demo",
                    "target": "${HOME}/.config/styler-demo",
                },
                verification={
                    "path": "${HOME}/.config/styler-demo/demo.txt",
                    "checksum": checksum,
                },
            ),
        ),
    )
    workflow = compile_recipe(recipe)
    graph = GraphDefinition(
        graph_id=recipe.recipe_id,
        title=recipe.name,
        description=recipe.description,
        workflow=workflow,
    )
    manifest = PackageManifest(
        package_id=package_id,
        name="Mi paquete importado",
        version="1.0.0",
        package_type=PackageType.CHANGE,
        description="Debe aparecer junto a PhotoGIMP.",
        artifacts=(
            ArtifactEntry("recipe", recipe.recipe_id, f"recipe/{recipe.recipe_id}.yaml", title=recipe.name),
            ArtifactEntry("graph", graph.graph_id, f"graph/{graph.graph_id}.json", title=graph.title),
            ArtifactEntry("asset", "demo-file", "assets/demo/demo.txt", title="demo.txt"),
        ),
    )
    destination = tmp_path / f"{package_id}.stylerpkg"
    build_package(
        manifest,
        {
            f"recipe/{recipe.recipe_id}.yaml": dumps_recipe(recipe).encode("utf-8"),
            f"graph/{graph.graph_id}.json": json.dumps(graph.to_dict(), ensure_ascii=False).encode("utf-8"),
            "assets/demo/demo.txt": payload,
        },
        destination,
    )
    change_id = ChangeService._portable_change_id(package_id, graph.graph_id)
    return destination, change_id, graph


def test_imported_package_appears_next_to_builtin_changes(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)

    cards = ChangeService(root=root, home=tmp_path / "home").available_changes()
    by_id = {card.change_id: card for card in cards}

    assert "photogimp" in by_id
    assert change_id in by_id
    assert by_id[change_id].name == "Mi paquete importado"
    assert by_id[change_id].provider_id == "stylerpkg"
    assert by_id[change_id].automation_level == "automatic"


def test_portable_plan_preserves_the_imported_dag(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, original_graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)

    service = ChangeService(root=root, home=tmp_path / "home")
    plan = service.build_plan(change_id)

    assert workflow_to_portable_dict(plan.workflow) == workflow_to_portable_dict(original_graph.workflow)
    assert plan.provider_id == "stylerpkg"
    assert plan.change_id == change_id


def test_deleting_available_package_removes_it_from_changes(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)
    service = ChangeService(root=root, home=tmp_path / "home")
    assert change_id in {card.change_id for card in service.available_changes()}

    removed = service.delete_available_change(change_id)
    assert removed == "Mi paquete importado"
    assert change_id not in {card.change_id for card in service.available_changes()}


def test_imported_dag_uses_change_execution_and_receipt_based_removal(tmp_path):
    root = tmp_path / "library"
    home = tmp_path / "home"
    home.mkdir()
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)
    service = ChangeService(root=root, home=home)

    result = service.execute(change_id)
    target = home / ".config/styler-demo/demo.txt"
    assert result.ok is True
    assert result.status == ChangeStatus.INTEGRATED
    assert target.read_text(encoding="utf-8") == "Styler portable DAG\n"
    assert service.can_rollback(change_id) is True
    assert change_id in {card.change_id for card in service.integrated_changes()}

    removed = service.rollback_change(change_id)
    assert removed.ok is True
    assert not target.exists()


def test_imported_change_has_no_second_provider_choice(tmp_path):
    root = tmp_path / "library"
    package_path, change_id, _graph = _portable_change(tmp_path)
    PortableLibrary(root).import_package(package_path)
    service = ChangeService(root=root, home=tmp_path / "home")

    options = service.provider_options(change_id)
    assert len(options) == 1
    assert options[0].provider_id == "stylerpkg"


def test_package_cli_no_longer_exposes_a_second_apply_path():
    from styler.cli import build_parser

    parser = build_parser()
    package_parser = next(
        action for action in parser._actions
        if getattr(action, "dest", None) == "command"
    ).choices["package"]
    action_subparser = next(
        action for action in package_parser._actions
        if getattr(action, "dest", None) == "action"
    )
    assert "plan" not in action_subparser.choices
    assert "run" not in action_subparser.choices
    assert {"list", "inspect", "import", "export", "delete"} <= set(action_subparser.choices)
    assert "enable" not in action_subparser.choices
    assert "disable" not in action_subparser.choices
