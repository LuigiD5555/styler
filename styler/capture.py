"""
styler.capture
=================
Orquesta los observers para producir una State y la persiste en
.styler/states/<id>.json. Cada observer puede fallar de forma aislada
(ver BaseObserver.safe_run) sin tumbar la captura completa.
"""

from __future__ import annotations

import json
import os
import platform
import uuid

from styler.applications import AppSpec, applications_from_inventory
from styler.desktop_environment import detect_desktop_environments
from styler.models import State
from styler.observers.apt_observer import AptObserver
from styler.observers.flatpak_observer import FlatpakObserver
from styler.observers.files_observer import FilesObserver
from styler.system_info import detect_distro

STYLER_DIR = ".styler"
STATES_DIR = os.path.join(STYLER_DIR, "states")


def _default_observers(scope: str = "plasma") -> list:
    return [AptObserver(), FlatpakObserver(), FilesObserver(scope=scope)]




def capture_applications(root: str = ".") -> list[AppSpec]:
    """Aplicaciones que la persona instaló, según el inventario de procedencia.

    Se compara contra la línea base cuando existe (`styler provenance-baseline
    set`). Si no hay línea base, se usa lo que el gestor declara como instalado
    a propósito. Es solo lectura: no descarga, no instala, no usa red.
    """
    try:
        from styler.provenance.baseline import load_baseline
        from styler.provenance.inventory import scan

        inventory, _problems = scan(scope="apps")
        baseline = load_baseline(root)
        return applications_from_inventory(inventory, baseline)
    except Exception as exc:  # noqa: BLE001 — una captura nunca debe caerse por esto
        print(f"[styler] no se pudo inventariar aplicaciones: {exc}")
        return []


def capture_state(
    label: str,
    observers: list | None = None,
    scope: str = "plasma",
    root: str = ".",
    with_applications: bool = True,
) -> State:
    observers = _default_observers(scope=scope) if observers is None else observers
    distro, base = detect_distro()

    state = State(state_id=str(uuid.uuid4())[:8], label=label, distro=distro, base=base)

    for obs in observers:
        state.packages.extend(obs.packages())
        state.files.extend(obs.files())
        state.services.extend(obs.services())

    state.desktop_environments = detect_desktop_environments(state.packages)
    state.desktops = [item.environment_id for item in state.desktop_environments]
    if with_applications:
        state.applications = capture_applications(root=root)
    return state


def save_state(state: State, root: str = ".") -> str:
    states_dir = os.path.join(root, STATES_DIR)
    os.makedirs(states_dir, exist_ok=True)
    path = os.path.join(states_dir, f"{state.state_id}.json")
    with open(path, "w") as fh:
        json.dump(state.to_dict(), fh, indent=2, ensure_ascii=False)
    return path


def load_state(state_id: str, root: str = ".") -> State:
    path = os.path.join(root, STATES_DIR, f"{state_id}.json")
    with open(path) as fh:
        return State.from_dict(json.load(fh))


def list_states(root: str = ".") -> list[str]:
    states_dir = os.path.join(root, STATES_DIR)
    if not os.path.isdir(states_dir):
        return []
    return sorted(f.removesuffix(".json") for f in os.listdir(states_dir) if f.endswith(".json"))
