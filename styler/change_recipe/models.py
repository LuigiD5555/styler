"""Receta semántica intermedia usada para sintetizar grafos de PipeCraft."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

RECIPE_SCHEMA = "styler.recipe/1"
SUPPORTED_OPERATIONS = frozenset({
    "package.install", "asset.overlay", "setting.apply",
    "release.fetch", "package.install_artifact", "executable.verify",
    "appimage.integrate", "appimage.verify",
})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class RecipeError(ValueError):
    pass


def _identifier(value: str, label: str) -> str:
    text = str(value).strip()
    if not _ID_RE.fullmatch(text):
        raise RecipeError(f"{label} inválido: {text}")
    return text


@dataclass(frozen=True)
class RecipeOperation:
    operation_id: str
    kind: str
    title: str
    config: Mapping[str, Any]
    needs: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    verification: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        if self.kind not in SUPPORTED_OPERATIONS:
            raise RecipeError(f"Operación no soportada: {self.kind}")
        if not self.title.strip():
            raise RecipeError("Cada operación necesita un título.")
        if not isinstance(self.config, Mapping):
            raise RecipeError("config debe ser un objeto.")
        if self.operation_id in self.needs:
            raise RecipeError(f"{self.operation_id} no puede depender de sí misma.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.operation_id, "kind": self.kind, "title": self.title,
            "config": dict(self.config), "needs": list(self.needs),
            "provides": list(self.provides), "requires": list(self.requires),
            "verification": dict(self.verification),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecipeOperation":
        if not isinstance(data, Mapping):
            raise RecipeError("Cada operación debe ser un objeto.")
        for key in ("config", "verification"):
            if data.get(key, {}) is not None and not isinstance(data.get(key, {}), Mapping):
                raise RecipeError(f"{key} debe ser un objeto.")
        return cls(
            operation_id=str(data.get("id", "")), kind=str(data.get("kind", "")),
            title=str(data.get("title", "")), config=dict(data.get("config") or {}),
            needs=tuple(str(x) for x in data.get("needs", []) or []),
            provides=tuple(str(x) for x in data.get("provides", []) or []),
            requires=tuple(str(x) for x in data.get("requires", []) or []),
            verification=dict(data.get("verification") or {}),
        )


@dataclass(frozen=True)
class ChangeRecipe:
    recipe_id: str
    name: str
    operations: tuple[RecipeOperation, ...]
    baseline_id: str = ""
    description: str = ""
    warnings: tuple[str, ...] = ()
    requires_changes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = RECIPE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RECIPE_SCHEMA:
            raise RecipeError(f"Esquema de receta no soportado: {self.schema}")
        _identifier(self.recipe_id, "recipe_id")
        if not self.name.strip():
            raise RecipeError("La receta necesita un nombre.")
        if not self.operations:
            raise RecipeError("La receta necesita al menos una operación.")
        ids = {item.operation_id for item in self.operations}
        if len(ids) != len(self.operations):
            raise RecipeError("Hay IDs de operación duplicados.")
        missing = sorted({need for item in self.operations for need in item.needs if need not in ids})
        if missing:
            raise RecipeError("Dependencias inexistentes: " + ", ".join(missing))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "recipe_id": self.recipe_id, "name": self.name,
            "description": self.description, "baseline_id": self.baseline_id,
            "warnings": list(self.warnings),
            "requires_changes": list(self.requires_changes),
            "metadata": dict(self.metadata),
            "operations": [item.to_dict() for item in self.operations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangeRecipe":
        if not isinstance(data, Mapping):
            raise RecipeError("La receta debe ser un objeto.")
        raw = data.get("operations") or []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise RecipeError("operations debe ser una lista.")
        return cls(
            schema=str(data.get("schema", "")), recipe_id=str(data.get("recipe_id", "")),
            name=str(data.get("name", "")), description=str(data.get("description", "")),
            baseline_id=str(data.get("baseline_id", "")),
            warnings=tuple(str(x) for x in data.get("warnings", []) or []),
            requires_changes=tuple(str(x) for x in data.get("requires_changes", []) or []),
            metadata=dict(data.get("metadata") or {}),
            operations=tuple(RecipeOperation.from_dict(x) for x in raw),
        )
