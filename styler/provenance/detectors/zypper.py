"""
styler.provenance.detectors.zypper
==================================
openSUSE Leap y Tumbleweed.

Evidencia usada:

* ``zypper --no-refresh --xmlout search --installed-only --type package`` —
  paquetes instalados y el alias del repositorio que los entrega.
* ``zypper --no-refresh lr --uri`` — alias, URL y si el repositorio está
  habilitado.
* ``/var/log/zypp/history`` — quién pidió la instalación. Las líneas con
  ``install`` marcadas por un usuario distinguen lo pedido de la dependencia
  arrastrada. Si el archivo no es legible, la razón queda ``UNKNOWN``: no se
  inventa.

``--no-refresh`` es obligatorio: refrescar repositorios usaría la red y la capa
de procedencia es de solo lectura y sin red.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    Confidence,
    InstallReason,
    Origin,
    OriginKind,
    Upstream,
)

ZYPP_HISTORY = "/var/log/zypp/history"


class ZypperDetector(Detector):
    name = "zypper"
    manager = "zypper"

    def __init__(
        self,
        runner: Runner | None = None,
        history_path: str | Path = ZYPP_HISTORY,
    ) -> None:
        super().__init__(runner)
        self.history_path = Path(history_path)

    def applies(self) -> bool:
        return self.runner.available("zypper")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        out = self.runner.run(
            [
                "zypper",
                "--no-refresh",
                "--xmlout",
                "search",
                "--installed-only",
                "--type",
                "package",
                "--details",
            ]
        )
        repositories = self._repositories()
        requested = self._user_requested()

        records: list[ApplicationRecord] = []
        for entry in parse_zypper_search(out):
            name = entry.get("name", "")
            if not name:
                continue
            alias = entry.get("repository", "")
            repository = repositories.get(alias, {})
            url = repository.get("url", "")

            if alias and url:
                origin = Origin(
                    kind=OriginKind.ZYPPER,
                    remote_name=alias,
                    remote_url=url,
                    confidence=Confidence.CONFIRMED,
                    evidence="zypper search --installed-only, zypper lr --uri",
                )
                install_method = "repository"
                warnings: list[str] = []
            else:
                origin = Origin(
                    kind=OriginKind.ZYPPER,
                    remote_name=alias,
                    confidence=Confidence.UNKNOWN,
                    evidence="zypper search --installed-only",
                )
                install_method = "unknown"
                warnings = [
                    "Ningún repositorio configurado entrega este paquete hoy. "
                    "Puede venir de un repositorio retirado o de un RPM local."
                ]

            if requested is None:
                reason = InstallReason.UNKNOWN
            elif name in requested:
                reason = InstallReason.EXPLICIT
            else:
                reason = InstallReason.DEPENDENCY

            records.append(
                ApplicationRecord(
                    app_id=f"zypper:{name}",
                    name=name,
                    display_name=entry.get("summary", "") or name,
                    manager="zypper",
                    version=entry.get("edition", ""),
                    architecture=entry.get("arch", ""),
                    install_method=install_method,
                    install_reason=reason,
                    category=AppCategory.DESKTOP_APP,
                    origin=origin,
                    upstream=Upstream(
                        confidence=Confidence.UNKNOWN,
                        evidence="zypper no declara el repositorio upstream en la búsqueda",
                    ),
                    warnings=warnings,
                )
            )
        return records

    def _repositories(self) -> dict[str, dict[str, str]]:
        try:
            out = self.runner.run(["zypper", "--no-refresh", "--xmlout", "lr", "--uri"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"zypper: no se pudieron listar repositorios ({exc})")
            return {}
        return parse_zypper_repositories(out)

    def _user_requested(self) -> set[str] | None:
        """Paquetes instalados a petición, según el historial de zypp.

        Devuelve ``None`` cuando el historial no se puede leer: eso significa
        «no se sabe», que no es lo mismo que «ninguno».
        """
        try:
            text = self.history_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        requested: set[str] = set()
        for raw in text.splitlines():
            fields = raw.split("|")
            if len(fields) < 3 or fields[1].strip() != "install":
                continue
            name = fields[2].strip()
            # El campo de usuario solicitante está al final; cuando zypp
            # instala una dependencia queda vacío.
            requester = fields[-1].strip() if len(fields) > 6 else ""
            if name and requester:
                requested.add(name)
        return requested


def parse_zypper_search(text: str) -> list[dict[str, str]]:
    """Convierte el XML de ``zypper search`` en registros planos."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    entries: list[dict[str, str]] = []
    for element in root.iter("solvable"):
        attributes = element.attrib
        if attributes.get("status", "installed") not in ("installed", "system"):
            continue
        entries.append(
            {
                "name": attributes.get("name", ""),
                "edition": attributes.get("edition", ""),
                "arch": attributes.get("arch", ""),
                "repository": attributes.get("repository", ""),
                "summary": attributes.get("summary", ""),
            }
        )
    return entries


def parse_zypper_repositories(text: str) -> dict[str, dict[str, str]]:
    """Convierte el XML de ``zypper lr --uri`` en un mapa alias → datos."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return {}
    repositories: dict[str, dict[str, str]] = {}
    for element in root.iter("repo"):
        alias = element.attrib.get("alias", "")
        if not alias:
            continue
        url = ""
        url_element = element.find("url")
        if url_element is not None and url_element.text:
            url = url_element.text.strip()
        repositories[alias] = {
            "url": url,
            "enabled": element.attrib.get("enabled", ""),
            "name": element.attrib.get("name", ""),
        }
    return repositories
