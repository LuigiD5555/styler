"""El catálogo TOML es ahora la única fuente de verdad de component_graph.

Esta prueba congela la cobertura del diccionario legado original: si alguien
borra una variante de un TOML, aquí falla. Sin esto, "migrar al catálogo"
podría perder silenciosamente el reconocimiento de un paquete.
"""
from __future__ import annotations

from styler.component_graph import CAPABILITY_PROVIDERS, _PACKAGE_INDEX, capabilities_for_package
from styler.models import Package

# El diccionario legado exacto, tal como estaba escrito a mano antes de 0.13.
LEGACY_COVERAGE = {
    "application.gimp": [
        ("apt", "gimp"), ("pacman", "gimp"), ("rpm", "gimp"), ("zypper", "gimp"),
        ("flatpak", "org.gimp.GIMP"), ("snap", "gimp"), ("appimage", "GIMP"),
    ],
    "application.konsole": [
        ("apt", "konsole"), ("pacman", "konsole"), ("rpm", "konsole"),
        ("zypper", "konsole"), ("flatpak", "org.kde.konsole"),
    ],
    "application.dolphin": [
        ("apt", "dolphin"), ("pacman", "dolphin"), ("rpm", "dolphin"),
        ("zypper", "dolphin"), ("flatpak", "org.kde.dolphin"),
    ],
    "desktop.kde-plasma": [
        ("apt", "kde-plasma-desktop"), ("apt", "kde-standard"), ("apt", "kubuntu-desktop"),
        ("apt", "plasma-desktop"), ("apt", "plasma-workspace"),
        ("pacman", "plasma-meta"), ("pacman", "plasma-desktop"), ("pacman", "plasma-workspace"),
        ("rpm", "plasma-desktop"), ("rpm", "plasma-workspace"),
        ("zypper", "patterns-kde-kde_plasma"), ("zypper", "plasma6-desktop"),
    ],
}


def test_catalogo_no_pierde_ninguna_variante_legada():
    missing = []
    for capability, pairs in LEGACY_COVERAGE.items():
        present = {(v.manager, v.package_name) for v in CAPABILITY_PROVIDERS.get(capability, ())}
        for pair in pairs:
            if pair not in present:
                missing.append((capability, pair))
    assert not missing, f"El catálogo TOML perdió cobertura legada: {missing}"


def test_paquetes_alias_siguen_reconociendose():
    # Si el usuario tiene plasma-workspace (no el metapaquete), sigue contando como KDE.
    assert capabilities_for_package(Package(manager="apt", name="plasma-workspace")) == ["desktop.kde-plasma"]
    assert capabilities_for_package(Package(manager="apt", name="kde-standard")) == ["desktop.kde-plasma"]


def test_config_root_se_conserva_por_gestor():
    variants = {v.manager: v for v in CAPABILITY_PROVIDERS["application.gimp"]}
    assert variants["apt"].config_root == "${HOME}/.config/GIMP"
    assert variants["flatpak"].config_root == "${HOME}/.var/app/org.gimp.GIMP/config/GIMP"
    assert variants["snap"].config_root == "${HOME}/snap/gimp/current/.config/GIMP"


def test_indice_de_paquetes_se_construyo_desde_el_catalogo():
    assert ("apt", "gimp") in _PACKAGE_INDEX
    assert ("flatpak", "org.gimp.gimp") in _PACKAGE_INDEX  # el índice normaliza a minúsculas
