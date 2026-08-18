"""Ejecución real de punta a punta: rutas, assets y aplicación de overlay."""
from __future__ import annotations

import tempfile
from pathlib import Path

from styler.component_catalog.compiler import compile_workflow
from styler.component_catalog.executors import extended_registry
from styler.component_catalog.loader import load
from styler.component_catalog.paths import (
    PathResolutionError,
    expand_user_path,
    resolve_catalog_uri,
)
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.planning.models import ExecutionContext, StepDefinition


def _registry() -> ComponentRegistry:
    return ComponentRegistry.from_report(load(root="."))


# --------------------------------------------------------------------------- #
# Rutas seguras
# --------------------------------------------------------------------------- #

def test_expand_user_path_expande_home():
    home = Path(tempfile.gettempdir()).resolve()
    assert expand_user_path("${HOME}/.config/GIMP", home=home) == home / ".config/GIMP"


def test_expand_user_path_rechaza_escape_del_home():
    home = Path(tempfile.gettempdir()).resolve() / "fakehome"
    home.mkdir(parents=True, exist_ok=True)
    try:
        expand_user_path("${HOME}/../../etc/passwd", home=home)
        assert False, "se esperaba PathResolutionError"
    except PathResolutionError:
        pass


def test_catalog_uri_resuelve_asset_real():
    asset = resolve_catalog_uri("catalog://photogimp")
    assert asset.is_dir()
    assert (asset / ".photogimp-marker").exists()


def test_catalog_uri_rechaza_escape_del_directorio_de_assets():
    try:
        resolve_catalog_uri("catalog://../../../etc")
        assert False, "se esperaba PathResolutionError"
    except PathResolutionError:
        pass


def test_catalog_uri_asset_inexistente_falla_explicito():
    try:
        resolve_catalog_uri("catalog://no-existe")
        assert False, "se esperaba PathResolutionError"
    except PathResolutionError as exc:
        assert "no existe" in str(exc)


# --------------------------------------------------------------------------- #
# El destino del overlay depende del proveedor de su dependencia
# --------------------------------------------------------------------------- #

def test_photogimp_fuerza_gimp_flatpak_por_default():
    registry = _registry()
    resolution = resolve(registry, ["app.photogimp"], family="ubuntu")
    compiled = compile_workflow(registry, resolution)
    by_id = {step.id: step for step in compiled.workflow.steps}
    assert resolution.selected_providers["app.gimp"] == "flatpak"
    assert by_id["app.photogimp.install"].config["target"] == (
        "${HOME}/.config/GIMP"
    )
    assert by_id["app.gimp.resolve-facts"].needs == ["app.gimp.install"]
    assert by_id["app.gimp.initialize"].needs == ["app.gimp.resolve-facts"]
    assert by_id["app.gimp.verify"].needs == ["app.gimp.initialize"]
    assert by_id["app.gimp.initialize"].config["application_id"] == "org.gimp.GIMP"
    assert by_id["app.gimp.initialize"].config["config_root"] == (
        "${HOME}/.var/app/org.gimp.GIMP/config/GIMP"
    )
    assert compiled.ok, [issue.to_dict() for issue in compiled.issues]


def test_photogimp_hereda_el_config_root_de_gimp_flatpak():
    registry = _registry()
    resolution = resolve(
        registry, ["app.photogimp"], family="ubuntu", preferred_providers={"app.gimp": "flatpak"}
    )
    compiled = compile_workflow(registry, resolution)
    by_id = {step.id: step for step in compiled.workflow.steps}
    # Cambiar el proveedor de GIMP cambia dónde escribe PhotoGIMP.
    assert by_id["app.photogimp.install"].config["target"] == (
        "${HOME}/.config/GIMP"
    )
    # La aplicación propietaria también conserva la ruta de SU proveedor. Esta
    # es la regresión que producía "Falta application_id o config_root".
    assert by_id["app.gimp.initialize"].config["config_root"] == (
        "${HOME}/.var/app/org.gimp.GIMP/config/GIMP"
    )


def test_config_kde_usa_su_propia_ruta_declarada():
    registry = _registry()
    resolution = resolve(registry, ["config.kde.user"], family="ubuntu")
    compiled = compile_workflow(registry, resolution)
    by_id = {step.id: step for step in compiled.workflow.steps}
    assert by_id["config.kde.user.install"].config["target"] == "${HOME}/.config"


# --------------------------------------------------------------------------- #
# Ejecución real del overlay (respaldo -> aplicar -> verificar)
# --------------------------------------------------------------------------- #

def test_overlay_se_aplica_de_verdad_sobre_el_directorio_destino():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        gimp_config = home / ".config" / "GIMP"
        gimp_config.mkdir(parents=True)
        (gimp_config / "preexistente.conf").write_text("mio", encoding="utf-8")

        registry = extended_registry()
        # HOME inyectado: prueba la escritura real sin tocar el HOME de verdad.
        ctx = ExecutionContext(root=Path(tmp), dry_run=False, values={"home": str(home)})

        # 1. Respaldo real de lo que ya había.
        backup = StepDefinition(
            id="app.photogimp.backup", step_type="backup_config",
            config={"backup_source": str(gimp_config)},
        )
        result = registry.get("backup_config").run(backup, ctx)
        assert result.success and result.data["existed"] is True
        assert (Path(result.data["backup"]) / "preexistente.conf").exists()

        # 2. Aplicación real del overlay desde el asset del catálogo.
        install = StepDefinition(
            id="app.photogimp.install", step_type="install_overlay",
            config={"source": "catalog://photogimp", "target": str(gimp_config)},
        )
        result = registry.get("install_overlay").run(install, ctx)
        assert result.success, result.message
        assert (gimp_config / ".photogimp-marker").exists()
        assert (gimp_config / "preexistente.conf").exists()  # no destruyó lo anterior

        # 3. Verificación real: el marcador existe -> la comprobación pasa.
        verify = StepDefinition(
            id="app.photogimp.verify", step_type="verify",
            config={
                "checks": ["directory:user-config:gimp", "marker:photogimp"],
                "target": str(gimp_config),
            },
        )
        result = registry.get("verify").run(verify, ctx)
        assert result.success, result.message


def test_verificacion_falla_si_el_marcador_no_esta():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        target = home / "vacio"
        target.mkdir(parents=True)
        registry = extended_registry()
        ctx = ExecutionContext(root=Path(tmp), dry_run=False, values={"home": str(home)})
        verify = StepDefinition(
            id="x.verify", step_type="verify",
            config={"checks": ["marker:photogimp"], "target": str(target)},
        )
        result = registry.get("verify").run(verify, ctx)
        assert not result.success
        assert result.data["error_code"] == "VERIFICATION_FAILED"


def test_overlay_rechaza_destino_fuera_del_home():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        registry = extended_registry()
        ctx = ExecutionContext(root=Path(tmp), dry_run=False, values={"home": str(home)})
        install = StepDefinition(
            id="malicioso.install", step_type="install_overlay",
            config={"source": "catalog://photogimp", "target": "/etc/cron.d"},
        )
        result = registry.get("install_overlay").run(install, ctx)
        assert not result.success
        assert result.data["error_code"] == "UNRESOLVED_SOURCE_OR_TARGET"
