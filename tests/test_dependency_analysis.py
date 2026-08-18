from styler.planning.dependency_analysis import (
    analyze_dependencies,
    impacted_by_capability,
    impacted_steps,
)
from styler.planning.models import StepDefinition, WorkflowDefinition
from styler.planning.validation import validate_workflow


def _photogimp_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="gimp-photogimp",
        steps=[
            StepDefinition(
                id="gimp.install",
                step_type="note",
                provides=["application.gimp.installed"],
                exclusive_resources=["apt", "dpkg"],
            ),
            StepDefinition(
                id="gimp.verify",
                step_type="note",
                needs=["gimp.install"],
                provides=["application.gimp.verified"],
            ),
            StepDefinition(
                id="photogimp.install",
                step_type="note",
                needs=["gimp.verify"],
                requires=["application.gimp.verified"],
                provides=["application.photogimp.installed"],
                exclusive_resources=["user-config:gimp"],
            ),
            StepDefinition(
                id="photogimp.verify",
                step_type="note",
                needs=["photogimp.install"],
                requires=["application.photogimp.installed"],
            ),
            StepDefinition(id="vlc.install", step_type="note", provides=["application.vlc.installed"]),
        ],
    )


def test_photogimp_provider_is_ordered_before_consumer():
    report = analyze_dependencies(_photogimp_workflow())
    assert report.valid
    assert report.providers["application.gimp.verified"] == ["gimp.verify"]


def test_missing_provider_is_rejected_before_execution():
    workflow = WorkflowDefinition(
        name="broken",
        steps=[StepDefinition(id="photogimp.install", step_type="note", requires=["application.gimp.verified"])],
    )
    errors = validate_workflow(workflow, {"note"})
    assert any("ningún paso la proporciona" in error for error in errors)


def test_provider_without_dependency_edge_is_rejected():
    workflow = WorkflowDefinition(
        name="unordered",
        steps=[
            StepDefinition(id="gimp.verify", step_type="note", provides=["application.gimp.verified"]),
            StepDefinition(id="photogimp.install", step_type="note", requires=["application.gimp.verified"]),
        ],
    )
    errors = validate_workflow(workflow, {"note"})
    assert any("no depende de su proveedor" in error for error in errors)


def test_removing_gimp_impacts_photogimp_but_not_independent_vlc():
    impacted = impacted_steps(_photogimp_workflow(), {"gimp.install"})
    assert impacted == ["gimp.install", "gimp.verify", "photogimp.install", "photogimp.verify"]
    assert "vlc.install" not in impacted


def test_removing_gimp_capability_reports_entire_dependent_branch():
    impacted = impacted_by_capability(_photogimp_workflow(), "application.gimp.verified")
    assert "gimp.verify" in impacted
    assert "photogimp.install" in impacted
    assert "photogimp.verify" in impacted
    assert "vlc.install" not in impacted


def test_alternative_provider_prevents_false_impact():
    workflow = WorkflowDefinition(
        name="alternatives",
        steps=[
            StepDefinition(id="gimp.apt", step_type="note", provides=["application.gimp.installed"]),
            StepDefinition(id="gimp.flatpak", step_type="note", provides=["application.gimp.installed"]),
            StepDefinition(
                id="photogimp.install",
                step_type="note",
                needs=["gimp.apt"],
                requires=["application.gimp.installed"],
            ),
        ],
    )
    impacted = impacted_steps(workflow, {"gimp.apt"})
    # La arista directa expresa que esta receta eligió apt; por eso PhotoGIMP
    # sigue afectado aunque exista un proveedor alternativo no seleccionado.
    assert impacted == ["gimp.apt", "photogimp.install"]
