"""Descubrimiento y persistencia de hechos observables de aplicaciones Flatpak.

La versión de una aplicación y su esquema de configuración no deben quedar
codificados en un DAG. Este módulo consulta la instalación real, normaliza la
versión de GIMP (por ejemplo 3.0.4 -> 3.0) y guarda esos hechos para que los
pasos posteriores consuman exactamente la misma evidencia.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from styler.runtime.commands import PipeCraftRunner


Runner = Callable[[Sequence[str]], Any]
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.\d+)*(?!\d)")
_MAJOR_ONLY_GIMP_VERSION = re.compile(r"(?:GIMP|version)\s+(\d+)(?![\d.])", re.IGNORECASE)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class FlatpakApplicationFacts:
    application_id: str
    installed: bool
    version: str = ""
    branch: str = ""
    architecture: str = ""
    origin: str = ""
    installation: str = ""
    ref: str = ""
    commit: str = ""
    config_schema: str = ""
    observed_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "FlatpakApplicationFacts":
        return cls(
            application_id=str(raw.get("application_id") or ""),
            installed=bool(raw.get("installed")),
            version=str(raw.get("version") or ""),
            branch=str(raw.get("branch") or ""),
            architecture=str(raw.get("architecture") or ""),
            origin=str(raw.get("origin") or ""),
            installation=str(raw.get("installation") or ""),
            ref=str(raw.get("ref") or ""),
            commit=str(raw.get("commit") or ""),
            config_schema=str(raw.get("config_schema") or ""),
            observed_at=float(raw.get("observed_at") or 0.0),
        )


def config_schema_from_version(version: str) -> str:
    """Convierte una versión completa a la carpeta de configuración esperada.

    GIMP 3.0.4 usa el esquema ``3.0`` y GIMP 2.10.38 usa ``2.10``. No se
    devuelve una conjetura cuando Flatpak no reporta una versión reconocible.
    """
    raw = version or ""
    match = _VERSION.search(raw)
    if match:
        return f"{int(match.group(1))}.{int(match.group(2))}"

    # Un futuro GIMP podría publicar una versión mayor sin parte menor en
    # alguna salida humana (por ejemplo ``GIMP 4``). La carpeta de
    # configuración se normaliza entonces como ``4.0``. No aceptamos cualquier
    # número aislado para evitar confundir arquitectura, commit u otros datos.
    major_only = _MAJOR_ONLY_GIMP_VERSION.search(raw)
    if major_only:
        return f"{int(major_only.group(1))}.0"
    return ""


def _default_runner(argv: Sequence[str]):
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return PipeCraftRunner(timeout=10).run(list(argv), timeout=10, env=env)


def _fields(line: str) -> list[str]:
    if "\t" in line:
        return [item.strip() for item in line.split("\t")]
    return line.split()


def _show_value(app_id: str, option: str, runner: Runner) -> str:
    probe = runner(["flatpak", "info", option, app_id])
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _probe_application_version(app_id: str, runner: Runner) -> str:
    """Obtiene la versión desde el propio binario cuando Flatpak no la publica.

    Algunas instalaciones antiguas dejan vacía la columna ``version``. Para
    GIMP, ``--version`` es una consulta sin interfaz gráfica y termina de
    inmediato, por lo que sirve como respaldo antes de abrir la aplicación.
    No se aplica genéricamente a otras aplicaciones porque no todas respetan
    ese contrato y algunas podrían iniciar su interfaz.
    """
    if app_id != "org.gimp.GIMP":
        return ""
    probe = runner(["flatpak", "run", app_id, "--version"])
    if probe.returncode != 0:
        return ""
    match = _VERSION.search(f"{probe.stdout}\n{probe.stderr}")
    return match.group(0) if match else ""


def inspect_flatpak_application(
    application_id: str,
    *,
    runner: Runner | None = None,
) -> FlatpakApplicationFacts:
    """Consulta la aplicación instalada sin abrirla ni modificar el equipo."""
    run = runner or _default_runner
    probe = run(
        [
            "flatpak",
            "list",
            "--app",
            "--columns=application,version,branch,arch,origin,installation",
        ]
    )
    row: list[str] | None = None
    if probe.returncode == 0:
        for raw in probe.stdout.splitlines():
            values = _fields(raw.strip())
            if values and values[0] == application_id:
                row = values
                break

    # Algunas versiones antiguas no admiten todas las columnas. La presencia
    # se confirma entonces con `flatpak info`; los demás datos se leen de su
    # salida en locale C cuando sea posible.
    info = run(["flatpak", "info", application_id])
    if row is None and info.returncode != 0:
        return FlatpakApplicationFacts(
            application_id=application_id,
            installed=False,
            observed_at=time.time(),
        )

    version = row[1] if row and len(row) > 1 else ""
    branch = row[2] if row and len(row) > 2 else ""
    architecture = row[3] if row and len(row) > 3 else ""
    origin = row[4] if row and len(row) > 4 else ""
    installation = row[5] if row and len(row) > 5 else ""

    labels: dict[str, str] = {}
    if info.returncode == 0:
        for raw in info.stdout.splitlines():
            key, separator, value = raw.partition(":")
            if separator:
                labels[key.strip().lower()] = value.strip()
    version = version or labels.get("version", "")
    version = version or _probe_application_version(application_id, run)
    branch = branch or labels.get("branch", "")
    origin = origin or labels.get("origin", "")
    installation = installation or labels.get("installation", "")

    ref = _show_value(application_id, "--show-ref", run)
    commit = _show_value(application_id, "--show-commit", run)
    if not branch and ref:
        parts = ref.split("/")
        if len(parts) >= 4:
            architecture = architecture or parts[-2]
            branch = parts[-1]

    return FlatpakApplicationFacts(
        application_id=application_id,
        installed=True,
        version=version,
        branch=branch,
        architecture=architecture,
        origin=origin,
        installation=installation,
        ref=ref,
        commit=commit,
        config_schema=config_schema_from_version(version),
        observed_at=time.time(),
    )


def facts_path(root: Path, application_id: str) -> Path:
    safe = _SAFE_ID.sub("_", application_id).strip("._") or "application"
    return Path(root) / ".styler" / "facts" / "flatpak" / f"{safe}.json"


def save_flatpak_facts(
    root: Path,
    facts: FlatpakApplicationFacts,
    *,
    preserve_existing: bool = True,
    **extra: object,
) -> Path:
    """Guarda los hechos observados sin destruir la evidencia ya acumulada.

    Los pasos del pipeline anotan evidencia adicional sobre este mismo archivo
    (por ejemplo ``initialization_completed`` o ``initialized_config_path``).
    Una consulta posterior de solo lectura —como la reconciliación de
    ``install_package``— reescribía el archivo con los campos base y borraba
    esa evidencia, de modo que el ciclo controlado de apertura y cierre de GIMP
    no podía demostrarse nunca.

    Los extras se conservan solo mientras describan la misma instalación. Si la
    versión o la ``ref`` cambian, la evidencia anterior deja de ser válida y se
    descarta para que el ciclo vuelva a ejecutarse de verdad.
    """
    path = facts_path(root, facts.application_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = facts.to_dict()
    if preserve_existing:
        previous = load_flatpak_facts(root, facts.application_id) or {}
        same_installation = (
            str(previous.get("version") or "") == facts.version
            and str(previous.get("ref") or "") == facts.ref
        )
        if same_installation:
            base_fields = set(FlatpakApplicationFacts.__dataclass_fields__)
            for key, value in previous.items():
                if key not in base_fields and key not in extra:
                    payload[key] = value
    payload.update(extra)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def load_flatpak_facts(root: Path, application_id: str) -> dict[str, object] | None:
    path = facts_path(root, application_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or str(raw.get("application_id") or "") != application_id:
        return None
    return raw
