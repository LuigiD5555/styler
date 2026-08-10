"""YAML interno y seguro para recetas; no es un formato público aparte."""
from __future__ import annotations

from typing import Any
import yaml

from .models import ChangeRecipe, RecipeError


def dumps_recipe(recipe: ChangeRecipe) -> str:
    return yaml.safe_dump(recipe.to_dict(), allow_unicode=True, sort_keys=False)


def loads_recipe(raw: str) -> ChangeRecipe:
    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RecipeError(f"YAML de receta inválido: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError("La receta YAML debe contener un objeto.")
    return ChangeRecipe.from_dict(data)
