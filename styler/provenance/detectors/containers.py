"""
styler.provenance.detectors.containers
======================================
Docker, Podman y Distrobox.

Un contenedor no es una aplicación del sistema: reproducirlo en otra máquina
significa recrear el entorno, no instalar un paquete. Por eso todo lo que
detecta este módulo queda con ``AppCategory.ISOLATED_ENV`` y la política de
exportación lo deja fuera del paquete salvo que la persona lo pida.

Evidencia usada: ``podman images --format json``, ``docker images --format
json`` y ``distrobox list --no-color``. Solo lectura: listar imágenes no
arranca nada.
"""
from __future__ import annotations

import json

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    Confidence,
    InstallReason,
    Origin,
    OriginKind,
)


class ContainerDetector(Detector):
    name = "containers"
    manager = "container"

    def applies(self) -> bool:
        return any(
            self.runner.available(program)
            for program in ("podman", "docker", "distrobox")
        )

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []
        for engine in ("podman", "docker"):
            if not self.runner.available(engine):
                continue
            try:
                out = self.runner.run([engine, "images", "--format", "json"])
            except Exception as exc:  # noqa: BLE001
                self.problems.append(f"{engine}: no se pudieron listar imágenes ({exc})")
                continue
            for image in parse_images(out):
                reference = image["reference"]
                records.append(
                    ApplicationRecord(
                        app_id=f"container:{engine}:{reference}",
                        name=reference,
                        display_name=reference,
                        manager="container",
                        version=image.get("tag", ""),
                        install_method=engine,
                        install_reason=InstallReason.EXPLICIT,
                        category=AppCategory.ISOLATED_ENV,
                        origin=Origin(
                            kind=OriginKind.CONTAINER,
                            remote_name=engine,
                            ref=reference,
                            commit=image.get("digest", ""),
                            confidence=Confidence.CONFIRMED,
                            evidence=f"{engine} images",
                        ),
                        warnings=[
                            "Es una imagen de contenedor. Reproducirla en otra "
                            "máquina significa volver a obtenerla, no instalar un "
                            "paquete del sistema."
                        ],
                    )
                )

        if self.runner.available("distrobox"):
            try:
                out = self.runner.run(["distrobox", "list", "--no-color"])
            except Exception as exc:  # noqa: BLE001
                self.problems.append(f"distrobox: no se pudo listar ({exc})")
                out = ""
            for box in parse_distrobox(out):
                records.append(
                    ApplicationRecord(
                        app_id=f"container:distrobox:{box['name']}",
                        name=box["name"],
                        display_name=f"distrobox «{box['name']}»",
                        manager="container",
                        install_method="distrobox",
                        install_reason=InstallReason.EXPLICIT,
                        category=AppCategory.ISOLATED_ENV,
                        origin=Origin(
                            kind=OriginKind.CONTAINER,
                            remote_name="distrobox",
                            ref=box.get("image", ""),
                            confidence=Confidence.CONFIRMED,
                            evidence="distrobox list",
                        ),
                    )
                )
        return records


def parse_images(text: str) -> list[dict[str, str]]:
    """Normaliza la salida JSON de ``podman images`` o ``docker images``.

    Podman devuelve una lista; Docker devuelve un objeto JSON por línea. Se
    aceptan las dos formas.
    """
    entries: list[dict[str, str]] = []
    raw_items: list = []

    stripped = text.strip()
    if not stripped:
        return entries
    try:
        parsed = json.loads(stripped)
        raw_items = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw_items.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        names = item.get("Names") or item.get("names") or []
        if isinstance(names, str):
            names = [names]
        reference = ""
        if names:
            reference = str(names[0])
        else:
            repository = str(item.get("Repository", ""))
            tag = str(item.get("Tag", ""))
            reference = f"{repository}:{tag}" if repository and tag else repository
        if not reference:
            continue
        entries.append(
            {
                "reference": reference,
                "tag": reference.rpartition(":")[2] if ":" in reference else "",
                "digest": str(item.get("Digest", "") or item.get("Id", "") or ""),
            }
        )
    return entries


def parse_distrobox(text: str) -> list[dict[str, str]]:
    """Convierte la tabla de ``distrobox list`` en registros."""
    entries: list[dict[str, str]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 2:
            continue
        if index == 0 and fields[0].upper() in ("ID", ""):
            continue
        name = fields[1] if len(fields) > 1 else ""
        if not name or name.upper() == "NAME":
            continue
        entries.append({"name": name, "image": fields[-1] if fields else ""})
    return entries
