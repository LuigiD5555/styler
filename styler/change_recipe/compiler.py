"""Compilador determinista de receta semántica a DAG PipeCraft."""
from __future__ import annotations

from styler.runtime.models import PhaseDefinition, StepDefinition, WorkflowDefinition, WorkflowOperation
from .models import ChangeRecipe, RecipeError


def _validate_satisfied_by(config: dict[str, object], operation_id: str) -> None:
    raw = config.get("satisfied_by")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise RecipeError(f"{operation_id}.satisfied_by debe ser un objeto.")
    executable = str(raw.get("executable") or "").strip()
    if not executable:
        raise RecipeError(f"{operation_id}.satisfied_by necesita executable.")


def compile_recipe(recipe: ChangeRecipe) -> WorkflowDefinition:
    steps: list[StepDefinition] = [
        StepDefinition(
            id="change.checkpoint",
            step_type="create_change_checkpoint",
            description="Registrar el punto inicial antes de modificar el sistema.",
            phase="prepare",
            config={"scope": "generated-change", "recipe_id": recipe.recipe_id},
            provides=["change.checkpoint.ready"],
        )
    ]
    verification_items: list[dict] = []
    operation_steps = {
        operation.operation_id: f"op.{operation.operation_id}" for operation in recipe.operations
    }
    for operation in recipe.operations:
        step_id = operation_steps[operation.operation_id]
        needs = [operation_steps[item] for item in operation.needs]
        if not needs:
            needs = ["change.checkpoint"]
        retries = 0
        retry_delay = 0.0
        if operation.kind == "package.install":
            manager = str(operation.config.get("manager", ""))
            name = str(operation.config.get("name", ""))
            if not manager or not name:
                raise RecipeError(f"{operation.operation_id} no declara manager y name.")
            config = {"package": dict(operation.config)}
            verification_items.append({"kind": "package", "manager": manager, "name": name})
            step_type = "install_package"
            # Toda instalación de software puede reintentarse una vez. El
            # ejecutor comprueba primero el estado real, así que si el primer
            # intento sí alcanzó a instalar el paquete pero falló al final, el
            # segundo intento lo reconcilia en vez de instalarlo otra vez.
            retries = 1
            retry_delay = 2.0
        elif operation.kind == "asset.overlay":
            source = str(operation.config.get("source", ""))
            target = str(operation.config.get("target", ""))
            if not source.startswith("package://") or not target:
                raise RecipeError(f"{operation.operation_id} necesita source package:// y target.")
            config = dict(operation.config)
            verification_items.append(
                {
                    "kind": "artifact",
                    "path": str(operation.verification.get("path") or target),
                    "checksum": str(operation.verification.get("checksum") or ""),
                }
            )
            step_type = "install_overlay"
        elif operation.kind == "setting.apply":
            config = dict(operation.config)
            backend = str(config.get("backend") or "")
            key = str(config.get("key") or "")
            value = str(config.get("value") or "")
            if backend not in {"gsettings", "kconfig"} or not key or not value:
                raise RecipeError(f"{operation.operation_id} declara un ajuste visual incompleto.")
            if backend == "gsettings" and not str(config.get("schema") or ""):
                raise RecipeError(f"{operation.operation_id} no declara el esquema de GSettings.")
            if backend == "kconfig" and (
                not str(config.get("schema") or "") or not str(config.get("group") or "")
            ):
                raise RecipeError(
                    f"{operation.operation_id} no declara archivo y grupo de KConfig."
                )
            verification_items.append({"kind": "setting", **config})
            step_type = "apply_visual_setting"
        elif operation.kind == "release.fetch":
            config = dict(operation.config)
            direct = str(config.get("url") or "")
            github = str(config.get("source") or "") == "github"
            if not direct.startswith("https://") and not github:
                raise RecipeError(
                    f"{operation.operation_id} necesita una URL HTTPS o source=github."
                )
            if github and not all(str(config.get(key) or "") for key in ("repository", "tag", "asset")):
                raise RecipeError(
                    f"{operation.operation_id} necesita repository, tag y asset para GitHub."
                )
            filename = str(config.get("filename") or config.get("asset") or "")
            _validate_satisfied_by(config, operation.operation_id)
            if not str(config.get("artifact_id") or "") or not filename:
                raise RecipeError(f"{operation.operation_id} necesita artifact_id y filename/asset.")
            config["filename"] = filename
            step_type = "fetch_release_artifact"
            retries = 1
            retry_delay = 2.0
        elif operation.kind == "package.install_artifact":
            config = dict(operation.config)
            if str(config.get("manager") or "") != "apt":
                raise RecipeError(f"{operation.operation_id} solo admite manager=apt en 0.9.11.")
            _validate_satisfied_by(config, operation.operation_id)
            if not str(config.get("artifact_id") or "") or not str(config.get("filename") or ""):
                raise RecipeError(f"{operation.operation_id} necesita artifact_id y filename.")
            step_type = "install_package_artifact"
            retries = 1
            retry_delay = 2.0
        elif operation.kind == "executable.verify":
            config = dict(operation.config)
            if not str(config.get("executable") or ""):
                raise RecipeError(f"{operation.operation_id} necesita executable.")
            step_type = "verify_executable"
            retries = 1
            retry_delay = 1.0
        elif operation.kind == "appimage.integrate":
            config = dict(operation.config)
            if not str(config.get("artifact_id") or "") or not str(config.get("filename") or ""):
                raise RecipeError(f"{operation.operation_id} necesita artifact_id y filename.")
            step_type = "integrate_appimage"
            retries = 1
            retry_delay = 2.0
        elif operation.kind == "appimage.verify":
            config = dict(operation.config)
            if not str(config.get("name_hint") or ""):
                raise RecipeError(f"{operation.operation_id} necesita name_hint.")
            step_type = "verify_appimage_integration"
            retries = 1
            retry_delay = 1.0
        else:
            raise RecipeError(f"Operación no soportada: {operation.kind}")
        steps.append(
            StepDefinition(
                id=step_id,
                step_type=step_type,
                description=operation.title,
                needs=needs,
                phase="apply",
                config=config,
                provides=list(operation.provides),
                requires=list(operation.requires),
                required=True,
                exclusive_resources=[f"recipe:{operation.operation_id}"],
                retries=retries,
                retry_delay=retry_delay,
            )
        )
    operation_step_ids = set(operation_steps.values())
    depended_on = {need for step in steps for need in step.needs if need in operation_step_ids}
    terminal_steps = sorted(operation_step_ids - depended_on)
    steps.append(
        StepDefinition(
            id="change.verify",
            step_type="verify_generated_change",
            description="Verificar que el cambio generado quedó aplicado.",
            needs=terminal_steps or ["change.checkpoint"],
            phase="verify",
            config={"checks": verification_items},
            required=True,
        )
    )
    return WorkflowDefinition(
        name=recipe.name,
        description=recipe.description,
        operation=WorkflowOperation.APPLY,
        steps=steps,
        phases={
            "prepare": PhaseDefinition(description="Preparación y checkpoint"),
            "apply": PhaseDefinition(description="Aplicación del cambio"),
            "verify": PhaseDefinition(description="Verificación final"),
        },
        metadata={
            "recipe_id": recipe.recipe_id,
            "baseline_id": recipe.baseline_id,
            "generated": True,
        },
    )
