"""
styler.provenance.detectors.flatpak
===================================
Flatpak es el mejor caso para procedencia: el propio gestor conoce el remote,
la rama, la referencia y el commit exacto de lo que está instalado.

Evidencia usada:

* ``flatpak list --app`` — aplicaciones, versión, rama, arquitectura, remote.
* ``flatpak remotes`` — URL y si el remote verifica firma GPG.
* ``flatpak info --show-commit`` / ``--show-ref`` — commit y referencia activa.

Nota importante: cuando el remote es Flathub, el repositorio de EMPAQUETADO es
``flathub/<app-id>`` por regla del propio remote. Eso no es el repositorio del
desarrollador y Styler no lo presenta como tal.
"""
from __future__ import annotations

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    Integrity,
    Origin,
    OriginKind,
    Upstream,
    InstallReason,
)
from styler.provenance.upstream import with_packaging_repository


class FlatpakDetector(Detector):
    name = "flatpak"
    manager = "flatpak"

    def __init__(self, runner: Runner | None = None) -> None:
        super().__init__(runner)

    def applies(self) -> bool:
        return self.runner.available("flatpak")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        remotes = self._remotes()
        out = self.runner.run(
            [
                "flatpak",
                "list",
                "--app",
                "--columns=application,version,branch,arch,origin,installation",
            ]
        )

        records: list[ApplicationRecord] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split("\t")]
            app_id = fields[0] if fields else ""
            if not app_id:
                continue
            version = fields[1] if len(fields) > 1 else ""
            branch = fields[2] if len(fields) > 2 else ""
            arch = fields[3] if len(fields) > 3 else ""
            remote_name = fields[4] if len(fields) > 4 else ""
            installation = fields[5] if len(fields) > 5 else "system"

            remote = remotes.get(remote_name, {})
            commit = self._commit(app_id)
            ref = self._ref(app_id) or (
                f"app/{app_id}/{arch}/{branch}" if arch and branch else ""
            )

            warnings: list[str] = []
            signed = remote.get("signed")
            if signed is False:
                warnings.append(
                    "El remote de Flatpak no verifica firmas GPG: "
                    "no se puede comprobar quién publicó esta versión."
                )
            if not remote_name:
                warnings.append("Flatpak no reportó el remote de origen.")

            origin = Origin(
                kind=OriginKind.FLATPAK,
                remote_name=remote_name,
                remote_url=remote.get("url", ""),
                branch=branch,
                ref=ref,
                commit=commit,
                channel=branch,
                vendor=remote_name,
                signed=signed,
                confidence=(
                    Confidence.CONFIRMED if remote_name and commit
                    else Confidence.INFERRED if remote_name
                    else Confidence.UNKNOWN
                ),
                evidence="flatpak list, flatpak remotes, flatpak info",
            )

            upstream = Upstream(confidence=Confidence.UNKNOWN, evidence="flatpak")
            if _is_flathub(remote_name, remote.get("url", "")):
                upstream = with_packaging_repository(
                    upstream,
                    f"flathub/{app_id}",
                    "regla del remote Flathub (repositorio de empaquetado, no de desarrollo)",
                )

            records.append(
                ApplicationRecord(
                    app_id=f"flatpak:{app_id}",
                    name=app_id,
                    display_name=app_id.split(".")[-1],
                    manager="flatpak",
                    version=version,
                    architecture=arch,
                    install_method=f"flatpak/{installation or 'system'}",
                    install_reason=InstallReason.EXPLICIT,
                    origin=origin,
                    upstream=upstream,
                    integrity=Integrity(signature_verified=signed),
                    warnings=warnings,
                )
            )
        return records

    # -- auxiliares ------------------------------------------------------

    def _remotes(self) -> dict[str, dict]:
        try:
            out = self.runner.run(
                ["flatpak", "remotes", "--columns=name,url,options"]
            )
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"flatpak: no se pudieron leer los remotes ({exc})")
            return {}
        remotes: dict[str, dict] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split("\t")]
            name = fields[0] if fields else ""
            if not name:
                continue
            url = fields[1] if len(fields) > 1 else ""
            options = fields[2] if len(fields) > 2 else ""
            remotes[name] = {
                "url": url,
                "options": options,
                "signed": "no-gpg-verify" not in options,
            }
        return remotes

    def _commit(self, app_id: str) -> str:
        try:
            return self.runner.run(["flatpak", "info", "--show-commit", app_id]).strip()
        except Exception:  # noqa: BLE001
            return ""

    def _ref(self, app_id: str) -> str:
        try:
            return self.runner.run(["flatpak", "info", "--show-ref", app_id]).strip()
        except Exception:  # noqa: BLE001
            return ""


def _is_flathub(remote_name: str, url: str) -> bool:
    return remote_name.lower() == "flathub" or "flathub.org" in url.lower()
