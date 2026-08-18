from __future__ import annotations

from ._support import *  # noqa: F401,F403

from .models import *  # noqa: F401,F403

def source_from_profile(profile_id: str, root: str = ".") -> RestoreSource:
    from styler.profiles import (
        compose,
        compose_applications,
        compose_profile,
        load_profile,
        load_profile_layers,
        unresolved_conflicts,
    )

    profile = load_profile(profile_id, root=root)
    layers = load_profile_layers(profile, root=root)
    applications = compose_applications(layers)

    # Un plan de componentes solo altera la restauración cuando la persona lo
    # confirmó. Un borrador sin confirmar sirve para explorar, nunca para
    # cambiar silenciosamente lo que se instalará o copiará.
    try:
        from styler.component_catalog.loader import load as load_component_catalog
        from styler.component_catalog.plan_draft import PlanDraftStore
        from styler.component_catalog.profile_bridge import (
            applications_for_draft,
            filter_layers_for_draft,
        )
        from styler.component_catalog.registry import ComponentRegistry

        draft = PlanDraftStore(root).load(profile_id)
        if draft is not None and draft.confirmed:
            registry = ComponentRegistry.from_report(load_component_catalog(root=root))
            original_layers = list(layers)
            layers = filter_layers_for_draft(original_layers, draft, registry)
            applications = applications_for_draft(original_layers, draft, registry)
    except (OSError, ValueError):
        # La validación visible del plan ya informa del problema. La lectura de
        # una configuración antigua no debe romperse por un archivo auxiliar.
        pass

    environment = ""
    for layer in layers:
        for record in layer.desktop_environments:
            environment = environment or record.environment_id
    # Con conflictos sin resolver todavía se puede *planear* (y verlos), pero la
    # capa de servicios no dejará aplicar hasta que se decidan.
    files = (
        compose(layers)
        if unresolved_conflicts(profile, layers)
        else compose_profile(profile, layers)
    )
    return RestoreSource(
        source_type="profile",
        source_id=profile_id,
        label=profile.name,
        environment_id=environment,
        applications=applications,
        files=files,
    )

def source_from_snapshot(snapshot_id: str, root: str = ".") -> RestoreSource:
    from styler.snapshot import load_snapshot

    snapshot = load_snapshot(snapshot_id, root=root)
    environment = ""
    for record in snapshot.state.desktop_environments:
        environment = environment or record.environment_id
    return RestoreSource(
        source_type="snapshot",
        source_id=snapshot_id,
        label=snapshot.label,
        environment_id=environment,
        applications=list(snapshot.state.applications),
        files=list(snapshot.state.files),
    )

def apply_restorable_base(source: RestoreSource, root: str = ".") -> RestoreSource:
    """Suma la base personal restaurable a lo que trae el perfil (ver styler/base.py)."""
    from styler import base as base_mod

    applications, environment = base_mod.merge_into_source(source, root)
    source.applications = applications
    source.environment_id = environment
    return source

def _restore_source(source_type: str, source_id: str, root: str) -> RestoreSource:
    if source_type == "snapshot":
        return source_from_snapshot(source_id, root=root)
    if source_type == "profile":
        return source_from_profile(source_id, root=root)
    raise ValueError(f"Tipo de origen de restauración desconocido: {source_type}")

def preview(
    source_type: str,
    source_id: str,
    root: str = ".",
    runner: Runner | None = None,
    privilege: str = "auto",
    target: target_mod.Target | None = None,
):
    """Vista de sólo aplicaciones para la CLI de inventario/restauración."""
    source = _restore_source(source_type, source_id, root)
    return apps_mod.plan_installation(
        source.applications,
        runner=runner,
        privilege=privilege,
        target=target,
        root=root,
    )
