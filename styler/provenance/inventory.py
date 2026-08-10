"""
styler.provenance.inventory
===========================
Construye y guarda el catálogo de procedencia.

Garantías de esta capa (Styler 0.8):

* SOLO LECTURA: no descarga, no instala, no modifica el sistema, no usa red.
* Un gestor roto no tumba el escaneo completo (cada detector se aísla).
* El inventario se guarda por contenido en ``.styler/provenance/``.
"""
from __future__ import annotations

import json
import os
import platform
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional

from styler.capture import detect_distro
from styler.provenance.detectors import (
    Detector,
    Runner,
    all_detectors,
)
from styler.provenance.models import ApplicationRecord, Inventory, SystemIdentity
from styler.provenance.artifacts import scan_visual_artifacts

PROVENANCE_DIRNAME = "provenance"
STYLER_DIR = ".styler"
LATEST = "latest.json"

ProgressHook = Optional[Callable[[str, int, int], None]]


class ProvenanceError(Exception):
    pass


def _dedupe(records: Iterable[ApplicationRecord]) -> list[ApplicationRecord]:
    """Un app_id, un registro. Gana el de mayor confianza de origen."""
    best: dict[str, ApplicationRecord] = {}
    for record in records:
        current = best.get(record.app_id)
        if current is None or record.origin.confidence.rank > current.origin.confidence.rank:
            best[record.app_id] = record
    return sorted(best.values(), key=lambda r: (r.manager, r.name.lower()))


def scan(
    scope: str = "apps",
    detectors: list[Detector] | None = None,
    runner: Runner | None = None,
    progress: ProgressHook = None,
    home: str | Path | None = None,
) -> tuple[Inventory, list[str]]:
    """Escanea la máquina y devuelve (inventario, problemas encontrados).

    Se ejecuta el registro único de detectores locales.
    """
    if scope not in ("apps", "all"):
        raise ProvenanceError("El alcance debe ser 'apps' o 'all'.")

    if detectors is None:
        detectors = all_detectors(runner, home=home)
    distro, _base = detect_distro()
    system = detect_system_identity()

    records: list[ApplicationRecord] = []
    managers_seen: list[str] = []
    problems: list[str] = []

    total = len(detectors)
    for index, detector in enumerate(detectors, start=1):
        if progress:
            progress(detector.name, index, total)
        if not detector.applies():
            continue
        managers_seen.append(detector.manager)
        records.extend(detector.detect(scope=scope))
        problems.extend(detector.problems)

    artifacts = []
    if scope == "all":
        artifacts, artifact_problems = scan_visual_artifacts(home=home, runner=runner)
        problems.extend(artifact_problems)

    inventory = Inventory(
        inventory_id=uuid.uuid4().hex[:8],
        captured_at=time.time(),
        distro=distro,
        system=system,
        scope=scope,
        managers_seen=sorted(set(managers_seen)),
        applications=_dedupe(records),
        artifacts=artifacts,
    )
    return inventory, problems


def detect_system_identity() -> SystemIdentity:
    info: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for raw in handle:
                if "=" not in raw:
                    continue
                key, value = raw.rstrip().split("=", 1)
                info[key] = value.strip().strip('"')
    except OSError:
        pass
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    distro_id = info.get("ID", "")
    rolling_ids = {
        "arch",
        "manjaro",
        "endeavouros",
        "garuda",
        "opensuse-tumbleweed",
        "tumbleweed",
    }
    release_model = "rolling" if distro_id.lower() in rolling_ids else "stable"
    return SystemIdentity(
        distro_id=distro_id,
        distro_version=info.get("VERSION_ID", ""),
        distro_variant=info.get("VARIANT_ID", info.get("VARIANT", "")),
        architecture=platform.machine(),
        desktop=desktop,
        desktop_version=(
            os.environ.get("STYLER_DESKTOP_VERSION", "")
            or os.environ.get("KDE_SESSION_VERSION", "")
        ),
        session_type=os.environ.get("XDG_SESSION_TYPE", ""),
        release_model=release_model,
        build_id=info.get("BUILD_ID", ""),
    )


# -- persistencia ---------------------------------------------------------


def provenance_dir(root: str | Path = ".") -> Path:
    path = Path(root) / STYLER_DIR / PROVENANCE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_inventory(inventory: Inventory, root: str | Path = ".") -> str:
    directory = provenance_dir(root)
    target = directory / f"{inventory.inventory_id}.json"
    payload = json.dumps(inventory.to_dict(), indent=2, ensure_ascii=False)

    temporary = target.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)

    pointer = directory / LATEST
    pointer_tmp = pointer.with_suffix(".tmp")
    pointer_tmp.write_text(
        json.dumps({"inventory_id": inventory.inventory_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    pointer_tmp.replace(pointer)
    return str(target)


def load_inventory(inventory_id: str, root: str | Path = ".") -> Inventory:
    path = provenance_dir(root) / f"{inventory_id}.json"
    if not path.is_file():
        raise ProvenanceError(f"No existe el inventario: {inventory_id}")
    return Inventory.from_dict(json.loads(path.read_text(encoding="utf-8")))


def latest_inventory(root: str | Path = ".") -> Optional[Inventory]:
    pointer = provenance_dir(root) / LATEST
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        return load_inventory(data["inventory_id"], root=root)
    except (OSError, ValueError, KeyError, ProvenanceError):
        return None


def list_inventories(root: str | Path = ".") -> list[str]:
    directory = provenance_dir(root)
    return sorted(
        path.stem
        for path in directory.glob("*.json")
        if path.name != LATEST and not path.name.endswith(".tmp")
    )
