"""
styler.provenance.detectors.apt
===============================
Procedencia para Debian / Ubuntu / Linux Mint.

Evidencia usada:

* ``dpkg-query`` — qué está instalado, versión, arquitectura, Homepage, Source.
* ``apt-cache policy`` — de qué repositorio salió exactamente la versión
  instalada (la línea marcada con ``***``).
* ``/etc/apt/sources.list`` y ``sources.list.d`` — URL y llave de cada fuente.
* ``/var/cache/apt/archives`` — si el ``.deb`` sigue en caché, la aplicación se
  puede reinstalar hoy sin red.

Un paquete cuya única fuente es ``/var/lib/dpkg/status`` fue instalado a mano o
viene de un repositorio que ya no está configurado: no hay remote que lo vuelva
a entregar. Eso se marca explícitamente.
"""
from __future__ import annotations

import re
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

DPKG_FORMAT = (
    "${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\t"
    "${Homepage}\t${source:Package}\t${Maintainer}\n"
)

_VERSION_LINE = re.compile(r"^\s{0,5}(\*\*\*)?\s*(\S+)\s+(\d+)\s*$")
_SOURCE_LINE = re.compile(r"^\s{6,}(\d+)\s+(\S+)\s*(.*)$")
_LOCAL_STATUS = "/var/lib/dpkg/status"

BATCH = 150


class AptDetector(Detector):
    name = "apt"
    manager = "apt"

    def __init__(
        self,
        runner: Runner | None = None,
        sources_dirs: list[str | Path] | None = None,
        applications_dirs: list[str | Path] | None = None,
        cache_dir: str | Path = "/var/cache/apt/archives",
    ) -> None:
        super().__init__(runner)
        self.sources_dirs = [Path(p) for p in (sources_dirs or ["/etc/apt"])]
        self.applications_dirs = [
            Path(p)
            for p in (
                applications_dirs
                or ["/usr/share/applications", "/usr/local/share/applications"]
            )
        ]
        self.cache_dir = Path(cache_dir)

    # -- disponibilidad -------------------------------------------------

    def applies(self) -> bool:
        return self.runner.available("dpkg-query")

    # -- detección ------------------------------------------------------

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        installed = self._installed_packages()
        if scope == "apps":
            wanted = self._application_packages()
            if wanted:
                installed = {n: v for n, v in installed.items() if n in wanted}

        policies = self._policies(list(installed))
        sources = self._sources_index()
        explicit = self._explicit_packages()

        records: list[ApplicationRecord] = []
        for name, meta in sorted(installed.items()):
            records.append(
                self._record(
                    name,
                    meta,
                    policies.get(name, {}),
                    sources,
                    explicit=explicit,
                )
            )
        return records

    # -- dpkg -----------------------------------------------------------

    def _installed_packages(self) -> dict[str, dict[str, str]]:
        out = self.runner.run(["dpkg-query", "-W", f"-f={DPKG_FORMAT}"])
        packages: dict[str, dict[str, str]] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, version, arch, status = parts[0], parts[1], parts[2], parts[3]
            if "installed" not in status:
                continue
            packages[name] = {
                "version": version,
                "architecture": arch,
                "homepage": parts[4] if len(parts) > 4 else "",
                "source": parts[5] if len(parts) > 5 else "",
                "maintainer": parts[6] if len(parts) > 6 else "",
            }
        return packages

    def _application_packages(self) -> set[str]:
        """Paquetes que aportan una aplicación visible en el menú."""
        desktop_files: list[str] = []
        for directory in self.applications_dirs:
            if directory.is_dir():
                desktop_files.extend(str(p) for p in sorted(directory.glob("*.desktop")))
        if not desktop_files:
            return set()

        owners: set[str] = set()
        for index in range(0, len(desktop_files), BATCH):
            chunk = desktop_files[index : index + BATCH]
            try:
                out = self.runner.run(["dpkg", "-S", *chunk])
            except Exception as exc:  # noqa: BLE001
                self.problems.append(f"apt: dpkg -S falló para un lote ({exc})")
                continue
            for line in out.splitlines():
                if ":" not in line:
                    continue
                owner = line.split(":", 1)[0]
                for pkg in owner.split(","):
                    pkg = pkg.strip().split(":")[0]
                    if pkg:
                        owners.add(pkg)
        return owners


    def _explicit_packages(self) -> set[str]:
        """Paquetes marcados como solicitados explícitamente por la persona."""
        if not self.runner.available("apt-mark"):
            return set()
        try:
            out = self.runner.run(["apt-mark", "showmanual"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"apt: no se pudo leer apt-mark showmanual ({exc})")
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    # -- apt-cache policy ------------------------------------------------

    def _policies(self, names: list[str]) -> dict[str, dict]:
        if not names or not self.runner.available("apt-cache"):
            return {}
        policies: dict[str, dict] = {}
        for index in range(0, len(names), BATCH):
            chunk = names[index : index + BATCH]
            try:
                out = self.runner.run(["apt-cache", "policy", *chunk])
            except Exception as exc:  # noqa: BLE001
                self.problems.append(f"apt: apt-cache policy falló para un lote ({exc})")
                continue
            policies.update(parse_apt_policy(out))
        return policies

    # -- sources ---------------------------------------------------------

    def _sources_index(self) -> dict[str, dict[str, str]]:
        index: dict[str, dict[str, str]] = {}
        for base in self.sources_dirs:
            candidates: list[Path] = []
            if (base / "sources.list").is_file():
                candidates.append(base / "sources.list")
            listd = base / "sources.list.d"
            if listd.is_dir():
                candidates.extend(sorted(listd.glob("*.list")))
                candidates.extend(sorted(listd.glob("*.sources")))
            for path in candidates:
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                if path.suffix == ".sources":
                    index.update(parse_deb822_sources(text, str(path)))
                else:
                    index.update(parse_one_line_sources(text, str(path)))
        return index

    # -- caché local -----------------------------------------------------

    def _cached_deb(self, name: str, version: str, arch: str) -> str:
        safe_version = version.replace(":", "%3a")
        candidate = self.cache_dir / f"{name}_{safe_version}_{arch}.deb"
        return str(candidate) if candidate.is_file() else ""

    # -- ensamblado -------------------------------------------------------

    def _record(
        self,
        name: str,
        meta: dict[str, str],
        policy: dict,
        sources: dict[str, dict[str, str]],
        explicit: set[str] | None = None,
    ) -> ApplicationRecord:
        version = meta["version"]
        arch = meta["architecture"]
        warnings: list[str] = []

        origin = Origin(
            kind=OriginKind.APT,
            vendor=meta.get("maintainer", ""),
            source_package=meta.get("source", "") or name,
            evidence="dpkg-query, apt-cache policy",
        )
        install_method = "repository"

        entry = policy.get("installed_from") if policy else None
        if entry and entry.get("url"):
            url = entry["url"]
            suite = entry.get("suite", "")
            origin.remote_url = url
            origin.remote_name = suite or url
            origin.branch = suite.split("/")[0] if suite else ""
            origin.confidence = Confidence.CONFIRMED
            source_meta = sources.get(_source_key(url, origin.branch))
            if source_meta:
                origin.signed = bool(source_meta.get("signed_by"))
                if not origin.signed:
                    warnings.append(
                        "La fuente APT no declara una llave de firma explícita."
                    )
            else:
                origin.signed = None
        elif entry and entry.get("local"):
            install_method = "manual"
            origin.confidence = Confidence.UNKNOWN
            origin.remote_name = "sin repositorio configurado"
            warnings.append(
                "Instalado a mano o desde un repositorio que ya no existe: "
                "hoy ningún remote puede volver a entregar esta versión."
            )
        else:
            origin.confidence = Confidence.UNKNOWN
            warnings.append("apt-cache policy no reportó el origen de esta versión.")

        cached = self._cached_deb(name, version, arch)
        integrity = Integrity(
            artifact_path=cached,
            artifact_available=bool(cached),
        )

        upstream = upstream_from_metadata(
            homepage=meta.get("homepage", ""),
            evidence="dpkg Homepage",
        )

        if install_method == "manual":
            install_reason = InstallReason.LOCAL
        elif explicit is not None and name in explicit:
            install_reason = InstallReason.EXPLICIT
        elif explicit is not None:
            install_reason = InstallReason.DEPENDENCY
        else:
            install_reason = InstallReason.UNKNOWN

        return ApplicationRecord(
            app_id=f"apt:{name}",
            name=name,
            display_name=name,
            manager="apt",
            version=version,
            architecture=arch,
            install_method=install_method,
            install_reason=install_reason,
            origin=origin,
            upstream=upstream,
            integrity=integrity,
            warnings=warnings,
        )


def _source_key(url: str, suite: str) -> str:
    return f"{url.rstrip('/')}|{suite}"


def parse_apt_policy(text: str) -> dict[str, dict]:
    """Extrae, por paquete, la fuente de la versión instalada (línea ``***``)."""
    result: dict[str, dict] = {}
    current: str = ""
    in_installed_entry = False

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith(" ") and raw.rstrip().endswith(":"):
            current = raw.rstrip().removesuffix(":").split(":")[0]
            result[current] = {"installed_from": {}}
            in_installed_entry = False
            continue
        if not current:
            continue

        version_match = _VERSION_LINE.match(raw)
        if version_match and not raw.lstrip().startswith(("Installed:", "Candidate:")):
            in_installed_entry = bool(version_match.group(1))
            continue

        source_match = _SOURCE_LINE.match(raw)
        if source_match and in_installed_entry:
            location = source_match.group(2)
            rest = source_match.group(3).strip()
            entry = result[current]["installed_from"]
            if entry:
                continue  # nos quedamos con la fuente de mayor prioridad
            if location == _LOCAL_STATUS:
                entry["local"] = True
            else:
                suite = ""
                pieces = rest.split()
                if pieces:
                    suite = pieces[0]
                entry["url"] = location
                entry["suite"] = suite
                entry["priority"] = source_match.group(1)
    return result


def parse_one_line_sources(text: str, origin_file: str = "") -> dict[str, dict[str, str]]:
    """Analiza líneas clásicas ``deb [opciones] URL suite componentes``."""
    index: dict[str, dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith("deb"):
            continue
        options = ""
        if "[" in line and "]" in line:
            options = line[line.index("[") + 1 : line.index("]")]
            line = line[: line.index("[")] + line[line.index("]") + 1 :]
        parts = line.split()
        if len(parts) < 3:
            continue
        url, suite = parts[1], parts[2]
        signed_by = ""
        for option in options.split():
            if option.startswith("signed-by="):
                signed_by = option.split("=", 1)[1]
        index[_source_key(url, suite)] = {
            "url": url,
            "suite": suite,
            "signed_by": signed_by,
            "file": origin_file,
        }
    return index


def parse_deb822_sources(text: str, origin_file: str = "") -> dict[str, dict[str, str]]:
    """Analiza el formato moderno ``.sources`` (deb822)."""
    index: dict[str, dict[str, str]] = {}
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for raw in block.splitlines():
            if ":" not in raw or raw.startswith((" ", "\t", "#")):
                continue
            key, _, value = raw.partition(":")
            fields[key.strip().lower()] = value.strip()
        uris = fields.get("uris", "").split()
        suites = fields.get("suites", "").split()
        signed_by = fields.get("signed-by", "")
        for url in uris:
            for suite in suites:
                index[_source_key(url, suite)] = {
                    "url": url,
                    "suite": suite,
                    "signed_by": signed_by,
                    "file": origin_file,
                }
    return index
