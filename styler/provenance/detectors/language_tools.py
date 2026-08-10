"""
styler.provenance.detectors.language_tools
==========================================
Herramientas instaladas por encima del intérprete: pipx, cargo y npm global.

Esto importa precisamente por cómo está definida la línea base de Styler: el
punto de partida incluye Python y Rust porque son necesarios para ejecutar
Styler. Todo lo que se instala *encima* de eso ya es un cambio del usuario, y
``baseline.compare()`` lo marca como ``ADDED`` sin necesidad de lógica extra.

Evidencia usada:

* ``pipx list --json``.
* ``~/.cargo/.crates2.json`` — se lee el archivo en vez de ejecutar cargo:
  es más rápido y no requiere que cargo esté en PATH.
* ``npm ls -g --depth=0 --json``.

No se enumeran paquetes de conda ni de pip dentro de un entorno: eso pertenece
al entorno, no al sistema, y confundiría la lista de cambios.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    Confidence,
    InstallReason,
    Origin,
    OriginKind,
)

CRATES_FILE = ".cargo/.crates2.json"


class LanguageToolDetector(Detector):
    name = "language-tools"
    manager = "language-tool"

    def __init__(self, runner: Runner | None = None, home: str | Path | None = None) -> None:
        super().__init__(runner)
        self.home = Path(home) if home else Path(os.path.expanduser("~"))

    def applies(self) -> bool:
        return (
            self.runner.available("pipx")
            or self.runner.available("npm")
            or (self.home / CRATES_FILE).is_file()
        )

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []
        records.extend(self._pipx())
        records.extend(self._cargo())
        records.extend(self._npm())
        return records

    def _pipx(self) -> list[ApplicationRecord]:
        if not self.runner.available("pipx"):
            return []
        try:
            out = self.runner.run(["pipx", "list", "--json"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"pipx: no se pudo listar ({exc})")
            return []
        return [
            self._record("pipx", OriginKind.PIPX, entry["name"], entry.get("version", ""), "PyPI")
            for entry in parse_pipx(out)
        ]

    def _cargo(self) -> list[ApplicationRecord]:
        path = self.home / CRATES_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [
            self._record(
                "cargo", OriginKind.CARGO, entry["name"], entry.get("version", ""), entry.get("source", "")
            )
            for entry in parse_crates(text)
        ]

    def _npm(self) -> list[ApplicationRecord]:
        if not self.runner.available("npm"):
            return []
        try:
            out = self.runner.run(["npm", "ls", "-g", "--depth=0", "--json"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"npm: no se pudo listar global ({exc})")
            return []
        return [
            self._record("npm", OriginKind.NPM, entry["name"], entry.get("version", ""), "npm registry")
            for entry in parse_npm_global(out)
        ]

    def _record(
        self, tool: str, kind: OriginKind, name: str, version: str, remote: str
    ) -> ApplicationRecord:
        return ApplicationRecord(
            app_id=f"{tool}:{name}",
            name=name,
            display_name=name,
            manager=tool,
            version=version,
            install_method="repository",
            install_reason=InstallReason.EXPLICIT,
            category=AppCategory.DEV_TOOL,
            origin=Origin(
                kind=kind,
                remote_name=remote,
                confidence=Confidence.CONFIRMED if remote else Confidence.INFERRED,
                evidence=f"{tool}",
            ),
        )


def parse_pipx(text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    venvs = data.get("venvs", {}) if isinstance(data, dict) else {}
    entries: list[dict[str, str]] = []
    for name, payload in venvs.items():
        version = ""
        if isinstance(payload, dict):
            package = payload.get("metadata", {}).get("main_package", {})
            if isinstance(package, dict):
                version = str(package.get("package_version", ""))
        entries.append({"name": str(name), "version": version})
    return entries


def parse_crates(text: str) -> list[dict[str, str]]:
    """Lee ``~/.cargo/.crates2.json``.

    Las claves tienen la forma ``nombre versión (origen)``.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    installs = data.get("installs", {}) if isinstance(data, dict) else {}
    entries: list[dict[str, str]] = []
    for key in installs:
        parts = str(key).split(" ", 2)
        if not parts or not parts[0]:
            continue
        source = parts[2].strip("()") if len(parts) > 2 else ""
        entries.append(
            {
                "name": parts[0],
                "version": parts[1] if len(parts) > 1 else "",
                "source": source,
            }
        )
    return entries


def parse_npm_global(text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    dependencies = data.get("dependencies", {}) if isinstance(data, dict) else {}
    entries: list[dict[str, str]] = []
    for name, payload in dependencies.items():
        version = ""
        if isinstance(payload, dict):
            version = str(payload.get("version", ""))
        entries.append({"name": str(name), "version": version})
    return entries
