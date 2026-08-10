"""Carga el catálogo de componentes desde sus tres niveles.

Niveles y prioridad (el último gana):

1. oficial   — ``styler/catalog/components/`` (empaquetado con Styler)
2. proyecto  — ``<root>/styler-components/``
3. paquetes  — componentes de ``.stylerpkg`` registrados
4. usuario   — ``~/.config/styler/components/``

Cada nivel tiene su propio ``index.toml``. El loader nunca descubre
componentes por ``glob`` únicamente: el índice es la fuente de verdad de
qué existe, y el loader además reporta archivos ``.toml`` bajo el nivel que
no estén registrados en su índice (huérfanos), como advertencia.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from styler.component_catalog.errors import (
    CatalogIndexError,
    CatalogParseError,
    CatalogPathError,
    DuplicateComponentError,
)
from styler.component_catalog.models import (
    SOURCE_OFFICIAL,
    SOURCE_PROJECT,
    SOURCE_PACKAGE,
    SOURCE_USER,
    ComponentDefinition,
    ComponentSource,
)
from styler.component_catalog.schema import parse_component

INDEX_FILENAME = "index.toml"

OFFICIAL_ROOT = Path(__file__).resolve().parent.parent / "catalog" / "components"


def _project_root(root: str | Path) -> Path:
    return Path(root) / "styler-components"


def _user_root() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "styler" / "components"


@dataclass
class LoadedComponent:
    definition: ComponentDefinition
    source: ComponentSource


@dataclass
class LoadReport:
    """Resultado transparente de una carga: qué se cargó y qué se advirtió."""

    components: dict[str, LoadedComponent] = field(default_factory=dict)
    overrides: list[str] = field(default_factory=list)  # mensajes legibles
    orphan_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def get(self, component_id: str) -> ComponentDefinition | None:
        loaded = self.components.get(component_id)
        return loaded.definition if loaded else None


def _safe_join(base: Path, relative: str, index_path: Path) -> Path:
    """Resuelve ``relative`` bajo ``base`` e impide escapar de la raíz.

    No sigue symlinks fuera de la raíz sin una política explícita: se
    resuelve el símlink y se vuelve a comprobar que el destino real siga
    dentro de ``base``.
    """
    if os.path.isabs(relative):
        raise CatalogPathError(relative, "las rutas del índice deben ser relativas")
    candidate = (base / relative).resolve()
    try:
        base_resolved = base.resolve()
    except OSError as exc:
        raise CatalogPathError(str(base), f"raíz de catálogo inaccesible: {exc}") from exc
    if base_resolved not in candidate.parents and candidate != base_resolved:
        raise CatalogPathError(relative, f"escapa de la raíz del catálogo ({index_path})")
    return candidate


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise CatalogParseError(str(path), f"no se pudo leer: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CatalogParseError(str(path), str(exc)) from exc


def _load_level(level_root: Path, level: str, report: LoadReport) -> None:
    index_path = level_root / INDEX_FILENAME
    if not level_root.is_dir():
        return
    if not index_path.is_file():
        report.warnings.append(
            f"{level}: {level_root} existe pero no tiene {INDEX_FILENAME}; se ignora"
        )
        return

    index_data = _read_toml(index_path)
    entries: dict[str, str] = index_data.get("components", {})
    if not isinstance(entries, dict):
        raise CatalogIndexError("<index>", f"'components' debe ser una tabla en {index_path}")

    seen_paths: set[Path] = set()
    level_ids: dict[str, str] = {}  # component_id -> archivo (para detectar duplicados EN el mismo nivel)

    for component_id, relative_path in entries.items():
        if not isinstance(relative_path, str):
            raise CatalogIndexError(component_id, "la ruta debe ser una cadena")
        resolved = _safe_join(level_root, relative_path, index_path)
        if not resolved.is_file():
            raise CatalogIndexError(
                component_id, f"el archivo indexado no existe: {relative_path}"
            )
        seen_paths.add(resolved)

        data = _read_toml(resolved)
        definition = parse_component(data, str(resolved))
        if definition.id != component_id:
            raise CatalogIndexError(
                component_id,
                f"el índice declara '{component_id}' pero el archivo define id='{definition.id}'",
            )

        if component_id in level_ids:
            raise DuplicateComponentError(component_id, level_ids[component_id], str(resolved))
        level_ids[component_id] = str(resolved)

        source = ComponentSource(path=str(resolved), level=level)
        definition.source = source
        existing = report.components.get(component_id)
        if existing is not None:
            report.overrides.append(
                f"{component_id}: {existing.source.level} ({existing.source.path}) "
                f"reemplazado por {level} ({resolved})"
            )
            if existing.source.level == SOURCE_OFFICIAL:
                report.warnings.append(
                    f"{component_id}: un componente oficial fue reemplazado por el nivel '{level}'"
                )
            if existing.definition.schema_version != definition.schema_version:
                report.warnings.append(
                    f"{component_id}: schema_version cambió de "
                    f"{existing.definition.schema_version} a {definition.schema_version} al sobreescribir"
                )
        report.components[component_id] = LoadedComponent(definition=definition, source=source)

    # Archivos .toml bajo el nivel que no están en su índice: huérfanos.
    for path in level_root.rglob("*.toml"):
        if path.name == INDEX_FILENAME:
            continue
        if path.resolve() not in seen_paths:
            report.orphan_files.append(str(path))


def load(root: str | Path = ".") -> LoadReport:
    """Carga los tres niveles, en orden de prioridad ascendente.

    El nivel de usuario se carga último y por tanto gana sobre proyecto y
    oficial; el de proyecto gana sobre el oficial.
    """
    report = LoadReport()
    _load_level(OFFICIAL_ROOT, SOURCE_OFFICIAL, report)
    _load_level(_project_root(root), SOURCE_PROJECT, report)
    # Los paquetes portables se cargan después del proyecto y antes de los
    # overrides manuales del usuario. Importar no ejecuta nada: solo un paquete
    # registrado participa en el catálogo.
    try:
        from styler.portable.library import PortableLibrary

        for package_root in PortableLibrary(root).component_roots():
            _load_level(package_root, SOURCE_PACKAGE, report)
    except Exception as exc:
        report.warnings.append(f"package: no se pudieron cargar componentes portables: {exc}")
    _load_level(_user_root(), SOURCE_USER, report)
    return report
