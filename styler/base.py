"""
styler.base
===========
La **base personal restaurable**: la colección explícita de lo que siempre debe
volver a existir en cualquier equipo tuyo.

No confundir con la *línea base del sistema* (`styler/provenance/baseline.py`),
que sirve para comparar qué venía de fábrica. Esa compara; esta declara.

La diferencia importa: si Firefox venía con la distro y tú lo usas todos los
días, comparar inventarios lo descarta («no lo añadiste») y al restaurar en un
equipo limpio te quedas sin Firefox. Por eso esta base es una colección
explícita que se guarda, se edita y se versiona: no es la consecuencia de una
resta.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from styler.applications import AppSpec, merge_applications

FILENAME = "restorable-base.json"


@dataclass
class RestorableBase:
    """Lo que siempre se reinstala: el escritorio y tus aplicaciones."""

    desktop_environments: list[str] = field(default_factory=list)
    applications: list[AppSpec] = field(default_factory=list)
    updated_at: float = 0.0
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.desktop_environments and not self.applications

    def add_application(self, spec: AppSpec) -> bool:
        if any(item.app_id == spec.app_id for item in self.applications):
            return False
        self.applications.append(spec)
        self.applications = merge_applications([self.applications])
        return True

    def remove_application(self, app_id: str) -> bool:
        before = len(self.applications)
        self.applications = [item for item in self.applications if item.app_id != app_id]
        return len(self.applications) != before

    def add_desktop(self, environment_id: str) -> bool:
        if environment_id in self.desktop_environments:
            return False
        self.desktop_environments.append(environment_id)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "desktop_environments": list(self.desktop_environments),
            "applications": [app.to_dict() for app in self.applications],
            "updated_at": self.updated_at,
            "note": self.note,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RestorableBase":
        return RestorableBase(
            desktop_environments=list(data.get("desktop_environments", [])),
            applications=[AppSpec.from_dict(item) for item in data.get("applications", [])],
            updated_at=float(data.get("updated_at", 0.0)),
            note=str(data.get("note", "")),
        )


def path_for(root: str | Path = ".") -> Path:
    return Path(root) / ".styler" / FILENAME


def load(root: str | Path = ".") -> Optional[RestorableBase]:
    path = path_for(root)
    if not path.is_file():
        return None
    try:
        return RestorableBase.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"[styler] base personal ilegible: {exc}")
        return None


def save(base: RestorableBase, root: str | Path = ".") -> Path:
    base.updated_at = time.time()
    path = path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(base.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def build_from_system(root: str | Path = ".", scope: str = "apps") -> RestorableBase:
    """Propone una base a partir de lo que hay hoy en el equipo.

    Toma **todo lo que instalaste a propósito**, no solo lo añadido después de
    una línea base. La persona luego quita lo que no quiera con `styler base
    remove`: es una propuesta, no un decreto.
    """
    from styler.applications import applications_from_inventory
    from styler.desktop_environment import detect_desktop_environments
    from styler.provenance.inventory import scan

    inventory, _problems = scan(scope=scope)
    applications = applications_from_inventory(inventory)
    desktops = [record.environment_id for record in detect_desktop_environments()]
    return RestorableBase(desktop_environments=desktops, applications=applications)


def merge_into_source(
    source, root: str | Path = "."
) -> tuple[list[AppSpec], str]:
    """Aplica la base personal a una restauración.

    Lo guardado en el perfil y lo declarado en la base se suman: la base es lo
    que *siempre* debe existir, aunque el perfil venga de un equipo donde ya
    estaba y no se registró.
    """
    base = load(root)
    if base is None:
        return list(source.applications), source.environment_id
    applications = merge_applications([source.applications, base.applications])
    environment = source.environment_id or (
        base.desktop_environments[0] if base.desktop_environments else ""
    )
    return applications, environment
