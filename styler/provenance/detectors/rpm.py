"""
styler.provenance.detectors.rpm
===============================
Fedora, openSUSE y derivadas.

Evidencia usada:

* ``rpm -qa`` con formato explícito — nombre, versión, arquitectura, URL,
  vendor, paquete fuente y firma.
* ``dnf repoquery --installed --qf '%{name}\t%{from_repo}'`` cuando existe dnf:
  dice exactamente de qué repositorio salió cada paquete instalado.
* ``zypper --no-refresh repos --uri`` cuando existe zypper: URL de cada
  repositorio configurado, para resolver el vendor a una URL real.

Un RPM sin firma o sin repositorio de origen queda marcado: no se puede
reconstruir con confianza.
"""
from __future__ import annotations

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

RPM_FORMAT = (
    "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{URL}\t%{VENDOR}\t"
    "%{SOURCERPM}\t%{PACKAGER}\t%|SIGPGP?{firmado}:{sin-firma}|\n"
)


class RpmDetector(Detector):
    name = "rpm"
    manager = "rpm"

    def __init__(self, runner: Runner | None = None) -> None:
        super().__init__(runner)

    def applies(self) -> bool:
        return self.runner.available("rpm")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        out = self.runner.run(["rpm", "-qa", "--qf", RPM_FORMAT])
        repo_of = self._repo_of_installed()
        repo_urls = self._repository_urls()
        frontend = (
            "dnf" if self.runner.available("dnf")
            else "zypper" if self.runner.available("zypper")
            else "rpm"
        )

        records: list[ApplicationRecord] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            name, version, arch = fields[0], fields[1], fields[2]
            url = fields[3] if len(fields) > 3 else ""
            vendor = fields[4] if len(fields) > 4 else ""
            source_rpm = fields[5] if len(fields) > 5 else ""
            packager = fields[6] if len(fields) > 6 else ""
            signed = (fields[7].strip() == "firmado") if len(fields) > 7 else None

            warnings: list[str] = []
            if signed is False:
                warnings.append("Este RPM no tiene firma: no se puede verificar su origen.")

            repository = repo_of.get(name, "")
            origin = Origin(
                kind=OriginKind.RPM,
                remote_name=repository,
                remote_url=repo_urls.get(repository, ""),
                vendor=vendor or packager,
                source_package=source_rpm,
                signed=signed,
                confidence=Confidence.CONFIRMED if repository else Confidence.INFERRED,
                evidence="rpm -qa, dnf repoquery, zypper repos",
            )
            if not repository:
                warnings.append(
                    "No se pudo determinar de qué repositorio salió este paquete."
                )
                origin.confidence = (
                    Confidence.INFERRED if vendor else Confidence.UNKNOWN
                )

            records.append(
                ApplicationRecord(
                    app_id=f"rpm:{name}",
                    name=name,
                    display_name=name,
                    manager="rpm",
                    version=version,
                    architecture=arch,
                    install_method=(
                        f"{frontend}/repository" if repository else f"{frontend}/unknown"
                    ),
                    install_reason=InstallReason.UNKNOWN,
                    origin=origin,
                    upstream=upstream_from_metadata(homepage=url, evidence="rpm URL"),
                    integrity=Integrity(signature_verified=signed),
                    warnings=warnings,
                )
            )
        return records

    def _repo_of_installed(self) -> dict[str, str]:
        if not self.runner.available("dnf"):
            return {}
        try:
            out = self.runner.run(
                [
                    "dnf",
                    "repoquery",
                    "--installed",
                    "--qf",
                    "%{name}\t%{from_repo}",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"rpm: dnf repoquery falló ({exc})")
            return {}
        mapping: dict[str, str] = {}
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] and fields[1] != "@System":
                mapping[fields[0]] = fields[1]
        return mapping

    def _repository_urls(self) -> dict[str, str]:
        if not self.runner.available("zypper"):
            return {}
        try:
            out = self.runner.run(["zypper", "--no-refresh", "repos", "--uri"])
        except Exception:  # noqa: BLE001
            return {}
        urls: dict[str, str] = {}
        for line in out.splitlines():
            if "|" not in line:
                continue
            columns = [column.strip() for column in line.split("|")]
            if len(columns) < 2:
                continue
            alias = columns[1]
            uri = next((c for c in columns if c.startswith(("http", "https", "dir:", "file:"))), "")
            if alias and uri:
                urls[alias] = uri
        return urls
