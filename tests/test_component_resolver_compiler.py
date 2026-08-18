from __future__ import annotations

from styler.component_catalog import loader as loader_mod
from styler.component_catalog import resolver as resolver_mod
from styler.component_catalog import compiler as compiler_mod
from styler.component_catalog.registry import ComponentRegistry
from styler.planning.plan import compile_workflow as compile_runtime_plan
from tests.support.local_scheduler import ResourceTable


def _registry() -> ComponentRegistry:
    report = loader_mod.load(root=".")
    return ComponentRegistry.from_report(report)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def test_gimp_se_resuelve_por_apt_en_ubuntu():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp"], family="ubuntu")
    assert result.ok
    assert result.selected_providers["app.gimp"] == "apt"


def test_gimp_puede_seleccionarse_flatpak_por_preferencia():
    registry = _registry()
    result = resolver_mod.resolve(
        registry, ["app.gimp"], family="ubuntu", preferred_providers={"app.gimp": "flatpak"}
    )
    assert result.selected_providers["app.gimp"] == "flatpak"


def test_photogimp_acepta_cualquier_proveedor_que_entregue_gimp_verificado():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.photogimp"], family="ubuntu")
    assert "app.gimp" in result.selected_components
    assert result.ok


def test_photogimp_queda_bloqueado_sin_gimp():
    registry = ComponentRegistry()
    from styler.component_catalog.schema import parse_component

    registry.register(
        parse_component(
            {
                "schema_version": 1,
                "id": "app.photogimp",
                "name": "PhotoGIMP",
                "kind": "application_overlay",
                "requires": ["app.gimp.verified"],
            },
            "<test>",
        )
    )
    result = resolver_mod.resolve(registry, ["app.photogimp"], family="ubuntu")
    assert not result.ok
    assert "app.gimp.verified" in result.missing_capabilities


def test_vlc_permanece_independiente():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp", "app.photogimp", "app.vlc"], family="ubuntu")
    assert result.ok
    assert "app.vlc" in result.selected_components
    # VLC no aparece como candidato ni dependencia de ningún requisito de GIMP/PhotoGIMP.
    vlc_decisions = [d for d in result.decisions if d.requirement and "app.vlc" in d.candidates]
    assert not vlc_decisions


def test_proveedor_incompatible_genera_decision_legible():
    registry = _registry()
    # Familia sin proveedor APT y política que excluye Flatpak: ningún
    # proveedor de GIMP queda disponible.
    result = resolver_mod.resolve(
        registry, ["app.gimp"], family="freebsd", allowed_provider_types=frozenset({"apt"})
    )
    assert not result.ok
    reasons = [d.reason for d in result.decisions if d.component_id == "app.gimp"]
    assert any("compatible" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Compilador DAG
# ---------------------------------------------------------------------------

def test_gimp_precede_a_photogimp_y_verificacion_gimp_precede_a_photogimp():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp", "app.photogimp"], family="ubuntu")
    compiled = compiler_mod.compile_workflow(registry, result)
    assert compiled.ok
    by_id = {step.id: step for step in compiled.workflow.steps}
    # PhotoGIMP es un overlay: su primer paso real es el respaldo, que
    # depende de la verificación de GIMP (GIMP.verify -> PhotoGIMP.backup ->
    # PhotoGIMP.install -> PhotoGIMP.verify, tal como pide el encargo).
    photogimp_backup = by_id["app.photogimp.backup"]
    assert "app.gimp.verify" in photogimp_backup.needs
    assert by_id["app.photogimp.install"].needs == ["app.photogimp.backup"]


def test_vlc_puede_correr_en_rama_separada():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp", "app.photogimp", "app.vlc"], family="ubuntu")
    compiled = compiler_mod.compile_workflow(registry, result)
    by_id = {step.id: step for step in compiled.workflow.steps}
    vlc_install = by_id["app.vlc.install"]
    assert vlc_install.needs == []


def test_gimp_apt_y_vlc_apt_se_serializan_por_recursos():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp", "app.vlc"], family="ubuntu")
    compiled = compiler_mod.compile_workflow(registry, result)
    by_id = {step.id: step for step in compiled.workflow.steps}
    gimp_install = by_id["app.gimp.install"]
    vlc_install = by_id["app.vlc.install"]
    assert set(gimp_install.exclusive_resources) & set(vlc_install.exclusive_resources)
    # Con recursos exclusivos compartidos, el scheduler no puede tenerlos activos a la vez.
    plan = compile_runtime_plan(compiled.workflow)
    gimp_node = plan.node(gimp_install.id)
    vlc_node = plan.node(vlc_install.id)
    assert gimp_node is not None and vlc_node is not None
    table = ResourceTable()
    assert table.can_acquire(gimp_node)
    table.acquire(gimp_node)
    assert not table.can_acquire(vlc_node)


def test_kde_precede_a_configuracion_kde():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["desktop.kde.plasma", "config.kde.user"], family="ubuntu")
    compiled = compiler_mod.compile_workflow(registry, result)
    by_id = {step.id: step for step in compiled.workflow.steps}
    # config.kde.user también es una 'configuration': respalda antes de escribir.
    config_backup = by_id["config.kde.user.backup"]
    assert "desktop.kde.plasma.verify" in config_backup.needs


def test_un_fallo_de_gimp_no_bloquea_vlc_estructuralmente():
    registry = _registry()
    result = resolver_mod.resolve(registry, ["app.gimp", "app.photogimp", "app.vlc"], family="ubuntu")
    compiled = compiler_mod.compile_workflow(registry, result)
    by_id = {step.id: step for step in compiled.workflow.steps}
    # VLC no tiene ningún 'needs' que dependa, directa o indirectamente, de GIMP.
    def transitive_needs(step_id: str, seen: set[str]) -> set[str]:
        if step_id in seen:
            return seen
        seen.add(step_id)
        for need in by_id[step_id].needs:
            transitive_needs(need, seen)
        return seen

    vlc_deps = transitive_needs("app.vlc.verify", set())
    assert not any(dep.startswith("app.gimp") for dep in vlc_deps)


def test_compilador_no_produce_ciclos_con_catalogo_oficial():
    registry = _registry()
    result = resolver_mod.resolve(
        registry, ["app.gimp", "app.photogimp", "app.vlc", "desktop.kde.plasma", "config.kde.user"],
        family="ubuntu",
    )
    compiled = compiler_mod.compile_workflow(registry, result)
    assert compiled.ok, [issue.to_dict() for issue in compiled.errors]
    step_ids = [step.id for step in compiled.workflow.steps]
    assert len(step_ids) == len(set(step_ids))
