from __future__ import annotations

import tempfile
from pathlib import Path

from styler.component_catalog import errors as cc_errors
from styler.component_catalog import loader as loader_mod
from styler.component_catalog import validator as validator_mod
from styler.component_catalog.models import ComponentDefinition, ComponentSource
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.schema import parse_component

MINIMAL = {
    "schema_version": 1,
    "id": "app.demo",
    "name": "Demo",
    "kind": "application",
}


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _component(**overrides) -> ComponentDefinition:
    data = dict(MINIMAL)
    data.update(overrides)
    return parse_component(data, "<test>")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_loader_carga_catalogo_oficial_valido():
    report = loader_mod.load(root=".")
    assert "app.gimp" in report.components
    assert "app.photogimp" in report.components
    assert "app.vlc" in report.components
    assert "desktop.kde.plasma" in report.components
    assert "config.kde.user" in report.components
    gimp = report.get("app.gimp")
    assert gimp is not None
    assert gimp.name == "GIMP"


def test_loader_rechaza_toml_invalido():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "styler-components/index.toml", 'schema_version = 1\n[components]\n"x.y" = "x.toml"\n')
        _write(root, "styler-components/x.toml", "esto no es TOML válido [[[")
        try:
            loader_mod.load(root=root)
            assert False, "se esperaba CatalogParseError"
        except cc_errors.CatalogParseError:
            pass


def test_loader_rechaza_ruta_fuera_de_raiz():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root, "styler-components/index.toml",
            'schema_version = 1\n[components]\n"x.y" = "../../evil.toml"\n',
        )
        try:
            loader_mod.load(root=root)
            assert False, "se esperaba CatalogPathError"
        except cc_errors.CatalogPathError:
            pass


def test_loader_detecta_entrada_faltante_del_indice():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root, "styler-components/index.toml",
            'schema_version = 1\n[components]\n"x.y" = "no-existe.toml"\n',
        )
        try:
            loader_mod.load(root=root)
            assert False, "se esperaba CatalogIndexError"
        except cc_errors.CatalogIndexError:
            pass


def test_loader_detecta_archivo_huerfano():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "styler-components/index.toml", 'schema_version = 1\n[components]\n')
        _write(
            root, "styler-components/huerfano.toml",
            'schema_version = 1\nid = "x.orphan"\nname = "Orphan"\nkind = "application"\n',
        )
        report = loader_mod.load(root=root)
        assert any("huerfano.toml" in path for path in report.orphan_files)


def test_loader_conserva_ruta_y_origen():
    report = loader_mod.load(root=".")
    gimp = report.components["app.gimp"]
    assert gimp.source.level == "official"
    assert gimp.source.path.endswith("gimp.toml")


def test_loader_prioridad_usuario_proyecto_oficial():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root, "styler-components/index.toml",
            'schema_version = 1\n[components]\n"app.gimp" = "gimp-override.toml"\n',
        )
        _write(
            root, "styler-components/gimp-override.toml",
            'schema_version = 1\nid = "app.gimp"\nname = "GIMP (proyecto)"\nkind = "application"\n'
            'provides = ["app.gimp.installed", "app.gimp.verified"]\n',
        )
        report = loader_mod.load(root=root)
        gimp = report.get("app.gimp")
        assert gimp is not None
        assert gimp.name == "GIMP (proyecto)"
        assert report.components["app.gimp"].source.level == "project"
        assert any("reemplazado" in message for message in report.overrides)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_rechaza_id_duplicado():
    registry = ComponentRegistry()
    registry.register(_component())
    try:
        registry.register(_component())
        assert False, "se esperaba DuplicateComponentError"
    except cc_errors.DuplicateComponentError:
        pass


def test_registry_indexa_proveedores_y_consumidores():
    registry = ComponentRegistry()
    registry.register(_component(id="app.gimp", provides=["app.gimp.verified"]))
    registry.register(
        _component(id="app.photogimp", name="PhotoGIMP", requires=["app.gimp.verified"])
    )
    providers = registry.providers_for("app.gimp.verified")
    assert [p.id for p in providers] == ["app.gimp"]
    consumers = registry.consumers_of("app.gimp.verified")
    assert [c.id for c in consumers] == ["app.photogimp"]


def test_registry_encuentra_dependientes():
    registry = ComponentRegistry()
    registry.register(_component(id="app.gimp", provides=["app.gimp.verified"]))
    registry.register(
        _component(id="app.photogimp", name="PhotoGIMP", requires=["app.gimp.verified"])
    )
    dependents = registry.dependents_of("app.gimp")
    assert [d.id for d in dependents] == ["app.photogimp"]


def test_registry_override_reemplaza_indices():
    registry = ComponentRegistry()
    registry.register(_component(id="app.gimp", provides=["app.gimp.verified"]))
    registry.override(_component(id="app.gimp", name="GIMP nuevo", provides=["app.gimp.other"]))
    assert registry.get("app.gimp").name == "GIMP nuevo"
    assert not registry.providers_for("app.gimp.verified")
    assert [p.id for p in registry.providers_for("app.gimp.other")] == ["app.gimp"]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_validator_detecta_capacidad_sin_proveedor():
    registry = ComponentRegistry()
    registry.register(_component(id="app.photogimp", requires=["app.gimp.verified"]))
    report = validator_mod.validate(registry)
    codes = [issue.code for issue in report.errors]
    assert "MISSING_PROVIDER" in codes


def test_validator_detecta_dependencia_propia():
    registry = ComponentRegistry()
    registry.register(_component(id="app.gimp", requires=["app.gimp"]))
    report = validator_mod.validate(registry)
    assert "SELF_DEPENDENCY" in [issue.code for issue in report.errors]


def test_validator_detecta_ciclo():
    registry = ComponentRegistry()
    registry.register(_component(id="a", requires=["cap.b"], provides=["cap.a"]))
    registry.register(_component(id="b", requires=["cap.a"], provides=["cap.b"]))
    report = validator_mod.validate(registry)
    assert "DEPENDENCY_CYCLE" in [issue.code for issue in report.errors]


def test_validator_detecta_proveedor_desconocido():
    registry = ComponentRegistry()
    data = dict(MINIMAL)
    data["providers"] = {"curl_install": {"type": "curl_pipe_bash", "families": ["*"]}}
    registry.register(parse_component(data, "<test>"))
    report = validator_mod.validate(registry)
    assert "UNKNOWN_PROVIDER_TYPE" in [issue.code for issue in report.errors]


def test_validator_detecta_recurso_apt_faltante():
    registry = ComponentRegistry()
    data = dict(MINIMAL)
    data["providers"] = {"ubuntu_apt": {"type": "apt", "families": ["ubuntu"], "packages": ["demo"]}}
    registry.register(parse_component(data, "<test>"))
    report = validator_mod.validate(registry)
    assert "MISSING_IMPLIED_RESOURCE" in [issue.code for issue in report.warnings]


def test_validator_detecta_rollback_irreal():
    registry = ComponentRegistry()
    data = dict(MINIMAL)
    data["rollback"] = {"level": "full"}
    registry.register(parse_component(data, "<test>"))
    report = validator_mod.validate(registry)
    assert "MISSING_ROLLBACK_STRATEGY" in [issue.code for issue in report.errors]


def test_validator_detecta_verificacion_ausente():
    registry = ComponentRegistry()
    data = dict(MINIMAL)
    data["providers"] = {"ubuntu_apt": {"type": "apt", "families": ["ubuntu"], "packages": ["demo"]}}
    registry.register(parse_component(data, "<test>"))
    report = validator_mod.validate(registry)
    assert "MISSING_VERIFICATION" in [issue.code for issue in report.errors]


def test_validator_detecta_compatibilidad_invalida():
    registry = ComponentRegistry()
    data = dict(MINIMAL)
    data["compatibility"] = {"wayland": "quizas"}
    registry.register(parse_component(data, "<test>"))
    report = validator_mod.validate(registry)
    assert "INVALID_COMPATIBILITY_VALUE" in [issue.code for issue in report.errors]


def test_catalogo_oficial_es_valido():
    report = loader_mod.load(root=".")
    registry = ComponentRegistry.from_report(report)
    validation = validator_mod.validate(registry)
    assert validation.ok, [issue.to_dict() for issue in validation.errors]


def test_photogimp_queda_conectado_a_gimp_verificado():
    report = loader_mod.load(root=".")
    registry = ComponentRegistry.from_report(report)
    providers = registry.providers_for("app.gimp.verified")
    assert [p.id for p in providers] == ["app.gimp"]
    consumers = registry.consumers_of("app.gimp.verified")
    assert "app.photogimp" in [c.id for c in consumers]


def test_vlc_no_depende_de_gimp():
    report = loader_mod.load(root=".")
    vlc = report.get("app.vlc")
    assert vlc is not None
    assert "app.gimp.verified" not in vlc.requires
    assert vlc.requires == ()
