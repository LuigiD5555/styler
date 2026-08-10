"""
styler.provenance.detectors.nix
===============================
Nix con perfil imperativo, y presencia de home-manager.

Evidencia usada:

* ``nix-env --query --installed --json`` — lo que está en el perfil del
  usuario. Por definición son paquetes que la persona pidió: las dependencias
  viven en el store, no en el perfil, así que este comando ya trae el filtro de
  alto nivel incorporado.
* ``~/.local/state/nix/profiles/home-manager`` — si existe, hay una
  configuración declarativa. Styler NO la enumera paquete por paquete: eso
  daría una lista que no se restaura instalando cosas sueltas. Se reporta una
  sola entrada que apunta a la configuración.

Las derivaciones efímeras de ``nix-shell`` y de flakes no aparecen porque no
están en el perfil. Eso es correcto: nunca fueron instaladas.
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
    Integrity,
    InstallReason,
    Origin,
    OriginKind,
)

HOME_MANAGER_PROFILE = ".local/state/nix/profiles/home-manager"


class NixDetector(Detector):
    name = "nix"
    manager = "nix"

    def __init__(self, runner: Runner | None = None, home: str | Path | None = None) -> None:
        super().__init__(runner)
        self.home = Path(home) if home else Path(os.path.expanduser("~"))

    def applies(self) -> bool:
        return (
            self.runner.available("nix-env")
            or (self.home / ".nix-profile").exists()
            or (self.home / HOME_MANAGER_PROFILE).exists()
        )

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []

        if self.runner.available("nix-env"):
            try:
                out = self.runner.run(["nix-env", "--query", "--installed", "--json"])
            except Exception as exc:  # noqa: BLE001
                self.problems.append(f"nix: no se pudo consultar el perfil ({exc})")
                out = ""
            for entry in parse_nix_env(out):
                name = entry["name"]
                records.append(
                    ApplicationRecord(
                        app_id=f"nix:{name}",
                        name=name,
                        display_name=entry.get("pname", "") or name,
                        manager="nix",
                        version=entry.get("version", ""),
                        install_method="repository",
                        install_reason=InstallReason.EXPLICIT,
                        category=AppCategory.DEV_TOOL,
                        origin=Origin(
                            kind=OriginKind.NIX,
                            remote_name="nix profile",
                            ref=entry.get("store_path", ""),
                            confidence=Confidence.CONFIRMED,
                            evidence="nix-env --query --installed --json",
                        ),
                        integrity=Integrity(
                            artifact_path=entry.get("store_path", ""),
                            artifact_available=bool(entry.get("store_path")),
                        ),
                    )
                )

        home_manager = self.home / HOME_MANAGER_PROFILE
        if home_manager.exists():
            records.append(
                ApplicationRecord(
                    app_id="nix:home-manager-profile",
                    name="home-manager",
                    display_name="Configuración de home-manager",
                    manager="nix",
                    install_method="declarative",
                    install_reason=InstallReason.EXPLICIT,
                    category=AppCategory.ISOLATED_ENV,
                    origin=Origin(
                        kind=OriginKind.NIX,
                        remote_name="home-manager",
                        ref=str(home_manager),
                        confidence=Confidence.CONFIRMED,
                        evidence=f"existe {HOME_MANAGER_PROFILE}",
                    ),
                    warnings=[
                        "Esta máquina usa home-manager. Su contenido se reproduce "
                        "regenerando la configuración, no instalando paquetes uno "
                        "por uno; Styler no la enumera ni la exporta."
                    ],
                )
            )
        return records


def parse_nix_env(text: str) -> list[dict[str, str]]:
    """Convierte la salida JSON de ``nix-env --query`` en registros planos."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    entries: list[dict[str, str]] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        pname = str(value.get("pname", ""))
        entries.append(
            {
                "name": pname or str(key),
                "pname": pname,
                "version": str(value.get("version", "")),
                "store_path": _first_store_path(value),
            }
        )
    return entries


def _first_store_path(value: dict) -> str:
    outputs = value.get("outputs", {})
    if isinstance(outputs, dict):
        for path in outputs.values():
            if isinstance(path, str) and path:
                return path
    return ""
