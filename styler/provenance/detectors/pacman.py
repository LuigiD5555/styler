"""
styler.provenance.detectors.pacman
==================================
Arch, Manjaro, EndeavourOS.

Evidencia usada:

* ``pacman -Qi`` — paquetes instalados, versión, arquitectura, URL, packager,
  validación de firma.
* ``pacman -Sl`` — a qué repositorio (core, extra, multilib, chaotic…)
  pertenece cada nombre.
* ``pacman -Qm`` — paquetes "foráneos": AUR o compilados a mano. No hay remote
  oficial que los vuelva a entregar.
* ``/var/cache/pacman/pkg`` — si el ``.pkg.tar.zst`` sigue en caché, la
  aplicación se puede reinstalar hoy sin red.
"""
from __future__ import annotations

from pathlib import Path

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    Integrity,
    Origin,
    OriginKind,
    InstallReason,
)
from styler.provenance.upstream import upstream_from_metadata

_FIELD_MAP = {
    "name": "name",
    "nombre": "name",
    "version": "version",
    "versión": "version",
    "architecture": "architecture",
    "arquitectura": "architecture",
    "url": "url",
    "packager": "packager",
    "empaquetador": "packager",
    "validated by": "validated_by",
    "validado por": "validated_by",
    "description": "description",
    "descripción": "description",
}


class PacmanDetector(Detector):
    name = "pacman"
    manager = "pacman"

    def __init__(
        self,
        runner: Runner | None = None,
        cache_dir: str | Path = "/var/cache/pacman/pkg",
    ) -> None:
        super().__init__(runner)
        self.cache_dir = Path(cache_dir)

    def applies(self) -> bool:
        return self.runner.available("pacman")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        info = parse_pacman_info(self.runner.run(["pacman", "-Qi"]))
        repositories = self._repositories()
        foreign = self._foreign()
        explicit = self._explicit()

        records: list[ApplicationRecord] = []
        for entry in info:
            name = entry.get("name", "")
            if not name:
                continue
            version = entry.get("version", "")
            arch = entry.get("architecture", "")
            validated = entry.get("validated_by", "")
            warnings: list[str] = []

            repository = repositories.get(name, "")
            if name in foreign:
                origin = Origin(
                    kind=OriginKind.PACMAN,
                    remote_name="foráneo (AUR o compilado a mano)",
                    vendor=entry.get("packager", ""),
                    confidence=Confidence.UNKNOWN,
                    evidence="pacman -Qm",
                )
                install_method = "manual"
                warnings.append(
                    "Paquete foráneo: ningún repositorio configurado lo entrega. "
                    "Sin el archivo guardado no hay forma segura de reinstalarlo."
                )
            elif repository:
                origin = Origin(
                    kind=OriginKind.PACMAN,
                    remote_name=repository,
                    branch=repository,
                    vendor=entry.get("packager", ""),
                    signed="signature" in validated.lower(),
                    confidence=Confidence.CONFIRMED,
                    evidence="pacman -Qi, pacman -Sl",
                )
                install_method = "repository"
            else:
                origin = Origin(
                    kind=OriginKind.PACMAN,
                    vendor=entry.get("packager", ""),
                    confidence=Confidence.UNKNOWN,
                    evidence="pacman -Qi",
                )
                install_method = "unknown"
                warnings.append("pacman no reportó el repositorio de este paquete.")

            cached = self._cached(name, version, arch)
            if name in foreign:
                install_reason = InstallReason.LOCAL
            elif name in explicit:
                install_reason = InstallReason.EXPLICIT
            elif explicit:
                install_reason = InstallReason.DEPENDENCY
            else:
                install_reason = InstallReason.UNKNOWN

            records.append(
                ApplicationRecord(
                    app_id=f"pacman:{name}",
                    name=name,
                    display_name=name,
                    manager="pacman",
                    version=version,
                    architecture=arch,
                    install_method=install_method,
                    install_reason=install_reason,
                    origin=origin,
                    upstream=upstream_from_metadata(
                        homepage=entry.get("url", ""), evidence="pacman URL"
                    ),
                    integrity=Integrity(
                        signature_verified="signature" in validated.lower() or None,
                        artifact_path=cached,
                        artifact_available=bool(cached),
                    ),
                    warnings=warnings,
                )
            )
        return records

    def _repositories(self) -> dict[str, str]:
        try:
            out = self.runner.run(["pacman", "-Sl"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"pacman: no se pudo listar repositorios ({exc})")
            return {}
        mapping: dict[str, str] = {}
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                mapping[fields[1]] = fields[0]
        return mapping

    def _explicit(self) -> set[str]:
        try:
            out = self.runner.run(["pacman", "-Qqe"])
        except Exception:  # noqa: BLE001
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _foreign(self) -> set[str]:
        try:
            out = self.runner.run(["pacman", "-Qm"])
        except Exception:  # noqa: BLE001
            return set()
        return {line.split()[0] for line in out.splitlines() if line.split()}

    def _cached(self, name: str, version: str, arch: str) -> str:
        if not self.cache_dir.is_dir() or not name or not version:
            return ""
        for suffix in (".pkg.tar.zst", ".pkg.tar.xz"):
            candidate = self.cache_dir / f"{name}-{version}-{arch or 'x86_64'}{suffix}"
            if candidate.is_file():
                return str(candidate)
        matches = sorted(self.cache_dir.glob(f"{name}-{version}-*.pkg.tar.*"))
        matches = [m for m in matches if not m.name.endswith(".sig")]
        return str(matches[0]) if matches else ""


def parse_pacman_info(text: str) -> list[dict[str, str]]:
    """Convierte la salida de ``pacman -Qi`` en registros."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if ":" not in raw or raw.startswith((" ", "\t")):
            continue
        key, _, value = raw.partition(":")
        normalized = _FIELD_MAP.get(key.strip().lower())
        if normalized:
            current[normalized] = value.strip()
    if current:
        entries.append(current)
    return entries
