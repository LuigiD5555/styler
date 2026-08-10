"""
styler.interpreter
=====================
Traduce diferencias técnicas (RawChange) en componentes comprensibles
(Component), tal como describe el resumen: "instalar GIMP" es un
componente; "aplicar PhotoGIMP" es otro que depende del primero.

Reglas del MVP (deliberadamente simples y explicables, no un modelo
de ML):

1. Cada paquete agregado (apt/flatpak) es su propio componente.
2. Los archivos agregados/modificados que caen bajo una ruta conocida
   de configuración de una app instalada en este changeset (p.ej.
   ~/.config/GIMP cuando el paquete "gimp" fue agregado) se agrupan
   como un componente de "personalización de aplicación" que depende
   del componente del paquete.
3. Los archivos restantes se agrupan por su primer directorio
   significativo (p.ej. todo lo nuevo bajo ~/.themes/Sweet se vuelve
   un componente "Tema: Sweet").
4. Servicios agregados son su propio componente.

Cada regla es una función pequeña; agregar reglas nuevas (KDE, XFCE,
fuentes...) no requiere tocar las existentes.
"""

from __future__ import annotations

from collections import defaultdict

from styler.models import (
    RawChange, ChangeKind, Component, Changeset, FileEntry, ServiceEntry, Package,
)
from styler.component_graph import (
    ComponentType, capabilities_for_package, resolve_component_graph,
)

CATEGORY_BY_ROOT = {
    "/opt": "aplicaciones",
    "/usr/local": "herramientas",
    "${HOME}/.local/bin": "herramientas",
    "${HOME}/.themes": "apariencia",
    "${HOME}/.icons": "apariencia",
    "${HOME}/.fonts": "apariencia",
    "${HOME}/.config": "configuraciones",
}


def _category_for(path: str) -> str:
    for root, category in CATEGORY_BY_ROOT.items():
        if path.startswith(root):
            return category
    return "sin_clasificar"


def _app_config_dir_for(package_name: str) -> str | None:
    """Heurística MVP: nombre de paquete -> subcarpeta esperada en
    ~/.config. Cubre el caso de validación (GIMP) y es fácil de
    extender con más entradas conocidas."""
    known = {
        "gimp": "GIMP",
        "krita": "kritarc",
        "kdenlive": "kdenliverc",
    }
    return known.get(package_name.lower())


def interpret(base_state_id: str, target_state_id: str, changes: list[RawChange]) -> Changeset:
    changeset = Changeset(
        changeset_id=f"{base_state_id[:4]}-{target_state_id[:4]}",
        base_state=base_state_id,
        target_state=target_state_id,
    )

    package_components: dict[str, Component] = {}
    file_changes = [c for c in changes if c.kind in (ChangeKind.FILE_ADDED, ChangeKind.FILE_MODIFIED)]
    consumed_paths: set[str] = set()

    # Regla 1: cada paquete agregado es un componente propio.
    for change in changes:
        if change.kind != ChangeKind.PACKAGE_ADDED:
            continue
        comp_id = f"pkg-{change.detail.get('manager')}-{change.subject}"
        package = Package(
            manager=change.detail.get("manager", "apt"),
            name=change.subject,
            version=change.detail.get("version", ""),
            architecture=change.detail.get("architecture", ""),
        )
        comp = Component(
            component_id=comp_id,
            title=f"Instalar {change.subject}",
            category="aplicaciones",
            component_type=ComponentType.APPLICATION,
            provides=capabilities_for_package(package),
            packages=[package],
            verification=[{
                "type": "package-present",
                "manager": package.manager,
                "name": package.name,
                "version": package.version,
            }],
            human_summary=f"Instalaste {change.subject}.",
        )
        package_components[change.subject] = comp
        changeset.components.append(comp)

        # Regla 2: archivos de configuración conocidos de esta app ->
        # componente de personalización que depende del paquete.
        config_dir = _app_config_dir_for(change.subject)
        if config_dir:
            matching = [
                fc for fc in file_changes
                if f"/.config/{config_dir}" in fc.subject and fc.subject not in consumed_paths
            ]
            if matching:
                sub_id = f"customize-{change.subject}"
                overlay_files = [
                    FileEntry(
                        path=fc.subject,
                        checksum=fc.detail.get("checksum", ""),
                        size=fc.detail.get("size", 0),
                        owner_hint=fc.detail.get("owner_hint", "user"),
                    )
                    for fc in matching
                ]
                requirement = capabilities_for_package(package)[0]
                sub = Component(
                    component_id=sub_id,
                    title="PhotoGIMP" if change.subject.lower() == "gimp" else f"Personalizar {change.subject}",
                    category="aplicaciones",
                    component_type=ComponentType.APPLICATION_OVERLAY,
                    depends_on=[comp_id],
                    requires=[requirement],
                    files=overlay_files,
                    verification=[
                        {"type": "file-present", "path": entry.path, "checksum": entry.checksum}
                        for entry in overlay_files
                    ],
                    human_summary=f"Personalizaste {change.subject} (perfil tipo PhotoGIMP).",
                )
                changeset.components.append(sub)
                consumed_paths.update(fc.subject for fc in matching)

    # Regla 3: archivos restantes -> agrupar por directorio "significativo".
    groups: dict[str, list] = defaultdict(list)
    for fc in file_changes:
        if fc.subject in consumed_paths:
            continue
        groups[_group_key(fc.subject)].append(fc)

    for key, group in groups.items():
        comp_id = f"files-{_slug(key)}"
        comp = Component(
            component_id=comp_id,
            title=key,
            category=_category_for(group[0].subject),
            files=[FileEntry(path=fc.subject, checksum=fc.detail.get("checksum", ""),
                              size=fc.detail.get("size", 0),
                              owner_hint=fc.detail.get("owner_hint", "user")) for fc in group],
            human_summary=f"Se agregaron/cambiaron {len(group)} archivo(s) bajo {key}.",
        )
        changeset.components.append(comp)

    # Regla 4: servicios agregados.
    for change in changes:
        if change.kind != ChangeKind.SERVICE_ADDED:
            continue
        comp_id = f"service-{change.subject}"
        comp = Component(
            component_id=comp_id,
            title=f"Habilitar servicio {change.subject}",
            category="servicios",
            services=[ServiceEntry(name=change.subject, scope=change.detail.get("scope", "user"))],
            human_summary=f"Agregaste un servicio de {change.detail.get('scope', 'user')}: {change.subject}.",
        )
        changeset.components.append(comp)

    # Resuelve relaciones semánticas (requires/provides) y conserva el grafo
    # por IDs para el motor de ejecución. Los faltantes se reportan en planes y
    # recetas; aquí no se instala nada automáticamente.
    resolve_component_graph(changeset.components)
    return changeset


def _group_key(path: str) -> str:
    """Reduce una ruta a un directorio representativo de 2-3 niveles
    para agrupar archivos hermanos como un solo componente."""
    parts = [p for p in path.split("/") if p]
    if path.startswith("${HOME}"):
        parts = parts[1:]  # descarta "${HOME}"
        depth = 2
    else:
        depth = 2
    keep = parts[:depth] if len(parts) > depth else parts
    return "/" + "/".join(keep) if not path.startswith("${HOME}") else "${HOME}/" + "/".join(keep)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower()
