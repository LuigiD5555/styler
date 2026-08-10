"""Catálogo de cambios incorporados descritos con recetas YAML.

El YAML es formato de autoría interno: define intención y dependencias. El
runtime sigue siendo el mismo DAG/PipeCraft usado por el resto de Styler.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import platform

from styler.change_recipe import ChangeRecipe, loads_recipe


@dataclass(frozen=True)
class DeclarativeChange:
    change_id: str
    recipe: ChangeRecipe
    source: Path
    category: str
    provider_label: str
    description: str

    @property
    def requires_changes(self) -> tuple[str, ...]:
        return self.recipe.requires_changes

    @property
    def families(self) -> tuple[str, ...]:
        raw = self.recipe.metadata.get("families") or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(str(item).strip().lower() for item in raw if str(item).strip())

    @property
    def architectures(self) -> tuple[str, ...]:
        raw = self.recipe.metadata.get("architectures") or ()
        if isinstance(raw, str):
            raw = (raw,)
        return tuple(_normalize_arch(str(item)) for item in raw if str(item).strip())

    def compatibility_error(self, *, family: str = "", architecture: str = "") -> str:
        family = (family or "").strip().lower()
        architecture = _normalize_arch(architecture or platform.machine())
        if self.families and family not in self.families:
            expected = ", ".join(self.families)
            return f"Este cambio requiere una distribución de familia {expected}; este equipo usa {family or 'desconocida'}."
        if self.architectures and architecture not in self.architectures:
            expected = ", ".join(self.architectures)
            return f"Este cambio requiere arquitectura {expected}; este equipo usa {architecture or 'desconocida'}."
        return ""

    def compatible_with(self, *, family: str = "", architecture: str = "") -> bool:
        return not self.compatibility_error(family=family, architecture=architecture)


def _normalize_arch(value: str) -> str:
    value = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(value, value)


def catalog_root() -> Path:
    return Path(__file__).resolve().parent / "catalog" / "changes"


def load_declarative_changes(root: str | Path | None = None) -> dict[str, DeclarativeChange]:
    folder = Path(root) if root is not None else catalog_root()
    changes: dict[str, DeclarativeChange] = {}
    if not folder.is_dir():
        return changes
    for path in sorted(folder.glob("*.yaml")):
        recipe = loads_recipe(path.read_text(encoding="utf-8"))
        metadata: Mapping[str, Any] = recipe.metadata
        change_id = recipe.recipe_id
        if change_id in changes:
            raise ValueError(f"Cambio YAML duplicado: {change_id}")
        changes[change_id] = DeclarativeChange(
            change_id=change_id,
            recipe=recipe,
            source=path,
            category=str(metadata.get("category") or "Cambio declarativo · YAML"),
            provider_label=str(metadata.get("provider_label") or "DAG YAML incorporado"),
            description=recipe.description,
        )
    _validate_dependency_graph(changes)
    return changes


def _validate_dependency_graph(changes: Mapping[str, DeclarativeChange]) -> None:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(change_id: str, chain: tuple[str, ...] = ()) -> None:
        if change_id in done:
            return
        if change_id in visiting:
            raise ValueError("Ciclo entre cambios YAML: " + " -> ".join((*chain, change_id)))
        change = changes.get(change_id)
        if change is None:
            raise ValueError(f"Dependencia YAML inexistente: {change_id}")
        visiting.add(change_id)
        for required in change.requires_changes:
            visit(required, (*chain, change_id))
        visiting.remove(change_id)
        done.add(change_id)

    for change_id in changes:
        visit(change_id)


def dependency_order(change_id: str, changes: Mapping[str, DeclarativeChange]) -> tuple[str, ...]:
    if change_id not in changes:
        raise ValueError(f"Cambio YAML inexistente: {change_id}")
    ordered: list[str] = []
    seen: set[str] = set()

    def walk(item: str) -> None:
        if item in seen:
            return
        for required in changes[item].requires_changes:
            walk(required)
        seen.add(item)
        ordered.append(item)

    walk(change_id)
    return tuple(ordered)


__all__ = ["DeclarativeChange", "catalog_root", "dependency_order", "load_declarative_changes"]
