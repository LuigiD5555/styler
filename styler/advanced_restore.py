"""Restauración avanzada y explícita de aplicaciones.

Esta capacidad está desactivada por defecto. Styler nunca consulta repositorios,
acepta otra versión, cambia de gestor ni instala una aplicación sin que la
persona haya habilitado cada permiso correspondiente y vuelva a aprobar la
operación concreta.

Los gestores siguen resolviendo sus bibliotecas internas. Esta capa solamente:

* localiza versiones candidatas en cachés o repositorios configurados;
* prioriza la versión exacta y el mismo gestor;
* presenta alternativas, incluso anteriores, cuando se autorizó;
* construye comandos sin usar un shell;
* exige aprobación adicional para una versión o proveedor diferente.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Iterable, Sequence
from urllib.request import Request, urlopen

from styler.applications import apt_install_argv, apt_update_argv
from styler.component_graph import CAPABILITY_PROVIDERS, ProviderVariant
from styler.desktop_environment import KDE_INSTALL_URL, KDE_PROJECT_URL
from styler.github_releases import ReleaseFetcher, default_release_fetcher, github_deb_assets
from styler.provenance.models import ApplicationRecord
from styler.runtime.commands import PipeCraftRunner

SETTINGS_DIR = ".styler/settings"
SETTINGS_FILE = "advanced-restore.json"


class AdvancedRestoreError(Exception):
    """Error seguro y traducible de la restauración avanzada."""


@dataclass
class AdvancedRestoreSettings:
    """Permisos persistentes. Todo empieza apagado."""

    schema_version: int = 1
    enabled: bool = False
    allow_repository_lookup: bool = False
    allow_alternative_versions: bool = False
    allow_provider_change: bool = False
    allow_installation: bool = False
    prefer_exact_version: bool = True
    require_per_operation_confirmation: bool = True
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "AdvancedRestoreSettings":
        return AdvancedRestoreSettings(
            schema_version=int(data.get("schema_version", 1)),
            enabled=bool(data.get("enabled", False)),
            allow_repository_lookup=bool(data.get("allow_repository_lookup", False)),
            allow_alternative_versions=bool(data.get("allow_alternative_versions", False)),
            allow_provider_change=bool(data.get("allow_provider_change", False)),
            allow_installation=bool(data.get("allow_installation", False)),
            prefer_exact_version=bool(data.get("prefer_exact_version", True)),
            # No se permite desactivar esta garantía desde un archivo editado.
            require_per_operation_confirmation=True,
            updated_at=float(data.get("updated_at", time.time())),
        )


@dataclass(frozen=True)
class CommandOutput:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], float], CommandOutput]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class RestoreCandidate:
    candidate_id: str
    capability: str
    manager: str
    name: str
    version: str = ""
    architecture: str = ""
    source_type: str = "repository"  # repository | local-artifact | installed
    source: str = ""
    remote: str = ""
    branch: str = ""
    revision: str = ""
    artifact_path: str = ""
    artifact_url: str = ""
    asset_name: str = ""
    relation: str = "unknown"  # exact | older | newer | available | unknown
    same_provider: bool = True
    installable: bool = True
    official_project_url: str = ""
    official_install_url: str = ""
    source_verified: bool = False
    notes: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        return self.relation == "exact"

    @property
    def alternative(self) -> bool:
        return self.relation in {"older", "newer", "unknown"}

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "capability": self.capability,
            "manager": self.manager,
            "name": self.name,
            "version": self.version,
            "architecture": self.architecture,
            "source_type": self.source_type,
            "source": self.source,
            "remote": self.remote,
            "branch": self.branch,
            "revision": self.revision,
            "artifact_path": self.artifact_path,
            "artifact_url": self.artifact_url,
            "asset_name": self.asset_name,
            "relation": self.relation,
            "same_provider": self.same_provider,
            "installable": self.installable,
            "official_project_url": self.official_project_url,
            "official_install_url": self.official_install_url,
            "source_verified": self.source_verified,
            "notes": list(self.notes),
        }


def replace_candidate_version(candidate: RestoreCandidate, version: str) -> RestoreCandidate:
    """Copia una opción cambiando solo la versión solicitada."""
    data = asdict(candidate)
    data["version"] = version
    data["notes"] = tuple(candidate.notes)
    return RestoreCandidate(**data)


def replace_candidate_artifact(candidate: RestoreCandidate, artifact_path: str) -> RestoreCandidate:
    """Copia una opción remota después de descargarla a una ruta temporal."""
    data = asdict(candidate)
    data["artifact_path"] = artifact_path
    data["artifact_url"] = ""
    data["notes"] = tuple(candidate.notes)
    return RestoreCandidate(**data)


def _download_release_asset(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise AdvancedRestoreError("Styler rechazó una URL de descarga que no pertenece a GitHub.")
    request = Request(url, headers={"User-Agent": "Styler"})
    try:
        with urlopen(request, timeout=120) as response, destination.open("wb") as output:  # noqa: S310
            shutil.copyfileobj(response, output)
    except Exception as exc:
        raise AdvancedRestoreError(f"No se pudo descargar el instalador desde GitHub: {exc}") from exc
    if not destination.is_file() or destination.stat().st_size == 0:
        raise AdvancedRestoreError("GitHub no entregó un instalador válido.")


@dataclass
class CandidateSearchResult:
    requested_name: str
    desired_version: str
    candidates: list[RestoreCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def exact_available(self) -> bool:
        return any(candidate.exact and candidate.installable for candidate in self.candidates)

    def to_dict(self) -> dict:
        return {
            "requested_name": self.requested_name,
            "desired_version": self.desired_version,
            "exact_available": self.exact_available,
            "warnings": list(self.warnings),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class InstallResult:
    success: bool
    executed: bool
    message: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "executed": self.executed,
            "message": self.message,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def settings_path(root: str | Path = ".") -> Path:
    directory = Path(root) / SETTINGS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / SETTINGS_FILE


def load_settings(root: str | Path = ".") -> AdvancedRestoreSettings:
    path = settings_path(root)
    if not path.is_file():
        return AdvancedRestoreSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AdvancedRestoreSettings.from_dict(data)
    except (OSError, ValueError, TypeError):
        # Una configuración dañada nunca debe habilitar una capacidad sensible.
        return AdvancedRestoreSettings()


def save_settings(settings: AdvancedRestoreSettings, root: str | Path = ".") -> str:
    settings.updated_at = time.time()
    path = settings_path(root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)
    return str(path)


def configure_settings(
    root: str | Path = ".",
    *,
    enabled: bool | None = None,
    allow_repository_lookup: bool | None = None,
    allow_alternative_versions: bool | None = None,
    allow_provider_change: bool | None = None,
    allow_installation: bool | None = None,
    acknowledge_risk: bool = False,
) -> AdvancedRestoreSettings:
    """Actualiza permisos sensibles solo con reconocimiento explícito."""
    current = load_settings(root)
    requested_true = any(
        value is True
        for value in (
            enabled,
            allow_repository_lookup,
            allow_alternative_versions,
            allow_provider_change,
            allow_installation,
        )
    )
    if requested_true and not acknowledge_risk:
        raise AdvancedRestoreError(
            "Para activar la restauración avanzada debes reconocer sus riesgos explícitamente."
        )

    if enabled is not None:
        current.enabled = enabled
    if allow_repository_lookup is not None:
        current.allow_repository_lookup = allow_repository_lookup
    if allow_alternative_versions is not None:
        current.allow_alternative_versions = allow_alternative_versions
    if allow_provider_change is not None:
        current.allow_provider_change = allow_provider_change
    if allow_installation is not None:
        current.allow_installation = allow_installation

    if not current.enabled:
        # Apagar el interruptor principal revoca todos los permisos derivados.
        current.allow_repository_lookup = False
        current.allow_alternative_versions = False
        current.allow_provider_change = False
        current.allow_installation = False

    save_settings(current, root)
    return current


def default_runner(argv: Sequence[str], timeout: float = 20.0) -> CommandOutput:
    result = PipeCraftRunner(timeout=timeout).run(list(argv), timeout=timeout)
    return CommandOutput(result.returncode, result.stdout, result.stderr)


def candidates_for_application(
    record: ApplicationRecord,
    settings: AdvancedRestoreSettings,
    *,
    capability: str = "",
    root: str | Path = ".",
    runner: Runner = default_runner,
    which: Which = shutil.which,
    release_fetcher: ReleaseFetcher = default_release_fetcher,
) -> CandidateSearchResult:
    _require_enabled(settings)
    result = CandidateSearchResult(record.name, record.version)

    if record.integrity.artifact_available and record.integrity.artifact_path:
        path = Path(record.integrity.artifact_path).expanduser()
        if path.is_file():
            result.candidates.append(
                _candidate(
                    capability=capability,
                    manager=record.manager,
                    name=record.name,
                    version=record.version,
                    architecture=record.architecture,
                    source_type="local-artifact",
                    source="artefacto registrado por Styler",
                    artifact_path=str(path),
                    desired_version=record.version,
                    original_manager=record.manager,
                )
            )
        else:
            result.warnings.append(
                "El inventario recuerda un instalador local, pero el archivo ya no existe."
            )

    result.candidates.extend(
        _local_cache_candidates(
            record.manager,
            record.name,
            record.version,
            record.architecture,
            capability,
            root,
            original_manager=record.manager,
        )
    )

    if settings.allow_repository_lookup:
        result.candidates.extend(
            _repository_candidates(
                record.manager,
                record.name,
                record.version,
                record.architecture,
                capability,
                record.origin.remote_name,
                runner,
                which,
                original_manager=record.manager,
            )
        )
        github_assets, github_warnings = github_deb_assets(record, fetcher=release_fetcher)
        result.warnings.extend(github_warnings)
        for asset in github_assets:
            result.candidates.append(
                _candidate(
                    capability=capability,
                    manager="apt",
                    name=record.name,
                    version=asset.version,
                    architecture=asset.architecture,
                    source_type="github-release",
                    source=f"GitHub Releases oficial ({asset.repository})",
                    artifact_url=asset.download_url,
                    asset_name=asset.name,
                    desired_version=record.version,
                    original_manager=record.manager,
                    source_verified=True,
                    notes=("Instalador .deb publicado por el proyecto en GitHub Releases.",),
                )
            )
    else:
        result.warnings.append(
            "La consulta de repositorios está desactivada; solo se revisaron archivos locales y cachés."
        )

    if settings.allow_provider_change and capability:
        for variant in CAPABILITY_PROVIDERS.get(capability, ()):
            if variant.manager == record.manager and variant.package_name.lower() == record.name.lower():
                continue
            result.candidates.extend(
                _local_cache_candidates(
                    variant.manager,
                    variant.package_name,
                    record.version,
                    record.architecture,
                    capability,
                    root,
                    original_manager=record.manager,
                )
            )
            if settings.allow_repository_lookup:
                result.candidates.extend(
                    _repository_candidates(
                        variant.manager,
                        variant.package_name,
                        record.version,
                        record.architecture,
                        capability,
                        "",
                        runner,
                        which,
                        original_manager=record.manager,
                    )
                )

    result.candidates = _filter_and_rank(
        result.candidates,
        settings,
        desired_version=record.version,
        original_manager=record.manager,
    )
    if not result.candidates:
        result.warnings.append("No se encontró ninguna versión permitida por la configuración actual.")
    return result


def candidates_for_capability(
    capability: str,
    settings: AdvancedRestoreSettings,
    *,
    desired_version: str = "",
    preferred_manager: str = "",
    root: str | Path = ".",
    runner: Runner = default_runner,
    which: Which = shutil.which,
) -> CandidateSearchResult:
    _require_enabled(settings)
    variants = list(CAPABILITY_PROVIDERS.get(capability, ()))
    if not variants:
        return CandidateSearchResult(
            capability,
            desired_version,
            warnings=[f"Styler todavía no conoce proveedores para {capability}."],
        )

    if preferred_manager:
        variants.sort(key=lambda item: item.manager != preferred_manager)
    if not settings.allow_provider_change and preferred_manager:
        variants = [item for item in variants if item.manager == preferred_manager]

    result = CandidateSearchResult(capability, desired_version)
    original_manager = preferred_manager
    for variant in variants:
        result.candidates.extend(
            _local_cache_candidates(
                variant.manager,
                variant.package_name,
                desired_version,
                "",
                capability,
                root,
                original_manager=original_manager,
            )
        )
        if settings.allow_repository_lookup:
            result.candidates.extend(
                _repository_candidates(
                    variant.manager,
                    variant.package_name,
                    desired_version,
                    "",
                    capability,
                    "",
                    runner,
                    which,
                    original_manager=original_manager,
                )
            )

    if not settings.allow_repository_lookup:
        result.warnings.append(
            "La consulta de repositorios está desactivada; solo se revisaron archivos locales y cachés."
        )
    result.candidates = _filter_and_rank(
        result.candidates,
        settings,
        desired_version=desired_version,
        original_manager=original_manager,
    )
    if not result.candidates:
        result.warnings.append("No se encontró un proveedor permitido para esa capacidad.")
    return result


def install_candidate(
    candidate: RestoreCandidate,
    settings: AdvancedRestoreSettings,
    *,
    execute: bool = False,
    approve: bool = False,
    approve_alternative_version: bool = False,
    approve_provider_change: bool = False,
    destination_home: str | Path | None = None,
    runner: Runner = default_runner,
    which: Which = shutil.which,
    downloader: Callable[[str, Path], None] | None = None,
) -> InstallResult:
    """Instala exactamente la opción elegida; nunca escoge otra automáticamente."""
    _require_enabled(settings)
    if not settings.allow_installation:
        raise AdvancedRestoreError(
            "La instalación de aplicaciones está desactivada en la configuración avanzada."
        )
    if not candidate.installable:
        raise AdvancedRestoreError("La opción elegida es informativa y no se puede instalar automáticamente.")
    if candidate.capability == "desktop.kde-plasma" and not candidate.source_verified:
        raise AdvancedRestoreError(
            "Styler se negó a instalar Plasma porque la opción no está vinculada a la ruta oficial de KDE."
        )
    if candidate.alternative and not approve_alternative_version:
        raise AdvancedRestoreError(
            "La opción no coincide con la versión solicitada. Debes aprobar esa versión alternativa."
        )
    if not candidate.same_provider and not approve_provider_change:
        raise AdvancedRestoreError(
            "La opción cambia de gestor o proveedor. Debes aprobar expresamente ese cambio."
        )
    if not approve:
        raise AdvancedRestoreError("Debes aprobar expresamente esta instalación concreta.")

    if candidate.artifact_url:
        if not execute:
            filename = candidate.asset_name or Path(urlparse(candidate.artifact_url).path).name or f"{candidate.name}.deb"
            preview = replace_candidate_artifact(candidate, f"/tmp/{filename}")
            argv, _ = _install_command(preview, destination_home, which)
            if argv is None:
                raise AdvancedRestoreError(f"No hay un instalador automático seguro para {candidate.manager}:{candidate.name}.")
            return InstallResult(
                True,
                False,
                f"Se descargaría {filename} desde GitHub Releases y después se instalaría.",
                ("download", candidate.artifact_url, "&&", *argv),
            )
        with tempfile.TemporaryDirectory(prefix="styler-github-") as temp_dir:
            filename = candidate.asset_name or Path(urlparse(candidate.artifact_url).path).name or f"{candidate.name}.deb"
            target = Path(temp_dir) / filename
            (downloader or _download_release_asset)(candidate.artifact_url, target)
            downloaded = replace_candidate_artifact(candidate, str(target))
            return install_candidate(
                downloaded,
                settings,
                execute=True,
                approve=True,
                approve_alternative_version=True,
                approve_provider_change=True,
                destination_home=destination_home,
                runner=runner,
                which=which,
                downloader=downloader,
            )

    argv, portable_target = _install_command(candidate, destination_home, which)
    if portable_target is not None:
        if not execute:
            return InstallResult(
                True,
                False,
                f"Se copiaría {candidate.name} a {portable_target}.",
                ("copy", candidate.artifact_path, str(portable_target)),
            )
        source = Path(candidate.artifact_path)
        portable_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, portable_target)
        portable_target.chmod(portable_target.stat().st_mode | 0o111)
        return InstallResult(
            True,
            True,
            f"Aplicación portable instalada en {portable_target}.",
            ("copy", str(source), str(portable_target)),
            0,
        )

    if argv is None:
        raise AdvancedRestoreError(
            f"No hay un instalador automático seguro para {candidate.manager}:{candidate.name}."
        )
    refresh_argv = _repository_refresh_command(candidate, which)
    planned = tuple(refresh_argv + ["&&"] + argv) if refresh_argv else tuple(argv)
    if not execute:
        return InstallResult(
            True,
            False,
            "Plan listo; se actualizaría el índice oficial y se instalaría la versión más reciente disponible.",
            planned,
        )

    combined_stdout: list[str] = []
    combined_stderr: list[str] = []
    if refresh_argv:
        refresh = runner(refresh_argv, 900.0)
        combined_stdout.append(refresh.stdout)
        combined_stderr.append(refresh.stderr)
        if refresh.returncode != 0:
            return InstallResult(
                False, True,
                f"No se pudo actualizar el repositorio oficial antes de instalar {candidate.name}.",
                planned, refresh.returncode, "\n".join(combined_stdout), "\n".join(combined_stderr),
            )

    output = runner(argv, 1800.0)
    combined_stdout.append(output.stdout)
    combined_stderr.append(output.stderr)
    return InstallResult(
        output.returncode == 0,
        True,
        (
            f"Se aseguró la versión más reciente disponible de {candidate.name}."
            if output.returncode == 0
            else f"No se pudo instalar o actualizar {candidate.name} (código {output.returncode})."
        ),
        planned,
        output.returncode,
        "\n".join(combined_stdout),
        "\n".join(combined_stderr),
    )


def _require_enabled(settings: AdvancedRestoreSettings) -> None:
    if not settings.enabled:
        raise AdvancedRestoreError(
            "La restauración avanzada está desactivada. Actívala conscientemente antes de buscar o instalar aplicaciones."
        )


def _repository_hosts(source: str) -> set[str]:
    hosts: set[str] = set()
    for raw_url in re.findall(r"https?://[^\s|]+", source or "", flags=re.IGNORECASE):
        hostname = (urlparse(raw_url).hostname or "").rstrip(".").lower()
        if hostname:
            hosts.add(hostname)
    return hosts


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _official_repository_source(manager: str, source: str) -> bool:
    """Reconoce orígenes oficiales conocidos sin confiar en texto parecido.

    KDE recomienda obtener Plasma mediante una distribución. El nombre del
    paquete por sí solo no basta: también se valida el origen informado por el
    gestor para evitar PPA o repositorios de terceros. El gestor del sistema
    sigue siendo responsable de verificar firmas y metadatos del repositorio.
    """
    value = (source or "").strip().lower()
    if not value:
        return False
    if manager == "apt":
        official_domains = (
            "ubuntu.com",
            "debian.org",
            "linuxmint.com",
            "neon.kde.org",
        )
        return any(
            _host_matches(host, official_domains)
            for host in _repository_hosts(source)
        )
    if manager == "pacman":
        return value in {"core", "extra", "multilib", "kde-unstable"}
    if manager == "rpm":
        return value in {
            "fedora",
            "updates",
            "updates-testing",
            "rawhide",
            "fedora-cisco-openh264",
        }
    if manager == "zypper":
        known_aliases = {
            "repo-oss",
            "repo-update",
            "repo-update-oss",
            "main repository (oss)",
            "update repository (oss)",
        }
        return (
            value in known_aliases
            or value.startswith("opensuse-tumbleweed-oss")
            or any(
                _host_matches(host, ("download.opensuse.org",))
                for host in _repository_hosts(source)
            )
        )
    return False


def _candidate(
    *,
    capability: str,
    manager: str,
    name: str,
    version: str,
    architecture: str = "",
    source_type: str,
    source: str,
    desired_version: str,
    original_manager: str,
    remote: str = "",
    branch: str = "",
    revision: str = "",
    artifact_path: str = "",
    artifact_url: str = "",
    asset_name: str = "",
    installable: bool = True,
    source_verified: bool | None = None,
    notes: Iterable[str] = (),
) -> RestoreCandidate:
    relation = _version_relation(version, desired_version)
    payload = "\0".join(
        (
            capability,
            manager,
            name,
            version,
            architecture,
            source_type,
            source,
            remote,
            branch,
            revision,
            artifact_path,
            artifact_url,
            asset_name,
        )
    )
    candidate_id = "cand-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    known_official_kde_provider = capability == "desktop.kde-plasma" and any(
        variant.manager == manager and variant.package_name.lower() == name.lower()
        for variant in CAPABILITY_PROVIDERS.get(capability, ())
    )
    official_project_url = KDE_PROJECT_URL if known_official_kde_provider else ""
    official_install_url = KDE_INSTALL_URL if known_official_kde_provider else ""
    if source_verified is None:
        source_verified = bool(
            known_official_kde_provider
            and source_type == "repository"
            and _official_repository_source(manager, source)
        )
    final_notes = list(notes)
    if known_official_kde_provider and source_verified:
        final_notes.append(
            "Origen oficial de la distribución, siguiendo la ruta de instalación recomendada por KDE."
        )
    elif known_official_kde_provider:
        final_notes.append(
            "El paquete es conocido, pero Styler no pudo confirmar que el repositorio sea oficial."
        )
    return RestoreCandidate(
        candidate_id=candidate_id,
        capability=capability,
        manager=manager,
        name=name,
        version=version,
        architecture=architecture,
        source_type=source_type,
        source=source,
        remote=remote,
        branch=branch,
        revision=revision,
        artifact_path=artifact_path,
        artifact_url=artifact_url,
        asset_name=asset_name,
        relation=relation,
        same_provider=not original_manager or manager == original_manager,
        installable=installable,
        official_project_url=official_project_url,
        official_install_url=official_install_url,
        source_verified=source_verified,
        notes=tuple(final_notes),
    )


def _filter_and_rank(
    candidates: Iterable[RestoreCandidate],
    settings: AdvancedRestoreSettings,
    *,
    desired_version: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    unique: dict[str, RestoreCandidate] = {}
    for candidate in candidates:
        if candidate.alternative and desired_version and not settings.allow_alternative_versions:
            continue
        if original_manager and not candidate.same_provider and not settings.allow_provider_change:
            continue
        unique[candidate.candidate_id] = candidate

    relation_order = {"exact": 0, "available": 1, "older": 2, "newer": 3, "unknown": 4}
    source_order = {"local-artifact": 0, "github-release": 1, "repository": 2, "installed": 3}
    return sorted(
        unique.values(),
        key=lambda candidate: (
            relation_order.get(candidate.relation, 9),
            0 if candidate.source_verified else 1,
            0 if candidate.same_provider else 1,
            source_order.get(candidate.source_type, 9),
            _version_sort_key(candidate.version),
            candidate.manager,
            candidate.name,
        ),
    )


def _version_relation(version: str, desired: str) -> str:
    if desired and version == desired:
        return "exact"
    if not desired:
        return "available"
    if not version:
        return "unknown"
    current = _version_sort_key(version)
    target = _version_sort_key(desired)
    if current == target:
        return "exact"
    return "older" if current < target else "newer"


def _version_sort_key(version: str) -> tuple:
    """Orden aproximado y estable; la instalación final la valida el gestor."""
    tokens = re.findall(r"\d+|[A-Za-z]+", version or "")
    return tuple((0, int(token)) if token.isdigit() else (1, token.lower()) for token in tokens)


def _local_cache_candidates(
    manager: str,
    name: str,
    desired_version: str,
    architecture: str,
    capability: str,
    root: str | Path,
    *,
    original_manager: str,
) -> list[RestoreCandidate]:
    patterns: list[Path] = []
    root_path = Path(root)
    vault = root_path / ".styler" / "artifacts"
    manager = manager.lower()
    if manager == "apt":
        patterns.extend(Path("/var/cache/apt/archives").glob(f"{name}_*.deb"))
        patterns.extend(vault.rglob(f"{name}_*.deb") if vault.exists() else [])
    elif manager == "pacman":
        patterns.extend(Path("/var/cache/pacman/pkg").glob(f"{name}-*.pkg.tar.*"))
        patterns.extend(vault.rglob(f"{name}-*.pkg.tar.*") if vault.exists() else [])
    elif manager in {"rpm", "zypper"}:
        patterns.extend(vault.rglob(f"{name}-*.rpm") if vault.exists() else [])
    elif manager == "appimage":
        patterns.extend(vault.rglob("*.AppImage") if vault.exists() else [])

    result: list[RestoreCandidate] = []
    for path in patterns:
        version = _version_from_filename(manager, name, path.name) or desired_version
        result.append(
            _candidate(
                capability=capability,
                manager=manager,
                name=name,
                version=version,
                architecture=architecture,
                source_type="local-artifact",
                source="caché local" if ".styler" not in path.parts else "bóveda de Styler",
                artifact_path=str(path),
                desired_version=desired_version,
                original_manager=original_manager,
            )
        )
    return result


def _repository_candidates(
    manager: str,
    name: str,
    desired_version: str,
    architecture: str,
    capability: str,
    remote_hint: str,
    runner: Runner,
    which: Which,
    *,
    original_manager: str,
) -> list[RestoreCandidate]:
    manager = manager.lower()
    if manager == "apt" and which("apt-cache"):
        output = runner(["apt-cache", "madison", name], 20.0)
        return _parse_apt_madison(
            output.stdout,
            name,
            desired_version,
            architecture,
            capability,
            original_manager,
        )
    if manager == "pacman" and which("pacman"):
        output = runner(["pacman", "-Si", name], 20.0)
        return _parse_pacman_info(
            output.stdout,
            name,
            desired_version,
            architecture,
            capability,
            original_manager,
        )
    if manager == "flatpak" and which("flatpak"):
        remotes = [remote_hint] if remote_hint else _flatpak_remotes(runner)
        result: list[RestoreCandidate] = []
        for remote in remotes[:20]:
            output = runner(["flatpak", "remote-info", remote, name], 30.0)
            if output.returncode == 0:
                result.extend(
                    _parse_flatpak_info(
                        output.stdout,
                        remote,
                        name,
                        desired_version,
                        architecture,
                        capability,
                        original_manager,
                    )
                )
        return result
    if manager == "snap" and which("snap"):
        output = runner(["snap", "info", name], 30.0)
        return _parse_snap_info(
            output.stdout,
            name,
            desired_version,
            architecture,
            capability,
            original_manager,
        )
    if manager == "rpm" and which("dnf"):
        output = runner(["dnf", "--showduplicates", "list", "--available", name], 30.0)
        return _parse_dnf_list(
            output.stdout,
            name,
            desired_version,
            architecture,
            capability,
            original_manager,
        )
    if manager == "zypper" and which("zypper"):
        output = runner(
            ["zypper", "--non-interactive", "search", "--details", "--match-exact", name],
            30.0,
        )
        return _parse_zypper_search(
            output.stdout,
            name,
            desired_version,
            architecture,
            capability,
            original_manager,
        )
    return []


def _parse_apt_madison(
    text: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    result = []
    for raw in text.splitlines():
        columns = [column.strip() for column in raw.split("|")]
        if len(columns) < 3 or columns[0] != name:
            continue
        version, source = columns[1], columns[2]
        result.append(
            _candidate(
                capability=capability,
                manager="apt",
                name=name,
                version=version,
                architecture=architecture,
                source_type="repository",
                source=source,
                desired_version=desired,
                original_manager=original_manager,
            )
        )
    return result


def _parse_pacman_info(
    text: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    fields = _colon_fields(text)
    version = fields.get("version", "")
    repository = fields.get("repository", "")
    if not version:
        return []
    return [
        _candidate(
            capability=capability,
            manager="pacman",
            name=name,
            version=version,
            architecture=fields.get("architecture", architecture),
            source_type="repository",
            source=repository,
            remote=repository,
            desired_version=desired,
            original_manager=original_manager,
            notes=(
                "pacman instala la versión vigente del repositorio; las versiones antiguas requieren un paquete en caché.",
            ),
        )
    ]


def _flatpak_remotes(runner: Runner) -> list[str]:
    output = runner(["flatpak", "remotes", "--columns=name"], 20.0)
    if output.returncode != 0:
        return []
    return [line.strip() for line in output.stdout.splitlines() if line.strip()]


def _parse_flatpak_info(
    text: str,
    remote: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    fields = _colon_fields(text)
    version = fields.get("version", "")
    branch = fields.get("branch", "")
    commit = fields.get("commit", "")
    if not (version or branch or commit):
        return []
    return [
        _candidate(
            capability=capability,
            manager="flatpak",
            name=name,
            version=version,
            architecture=fields.get("arch", architecture),
            source_type="repository",
            source=remote,
            remote=remote,
            branch=branch,
            revision=commit,
            desired_version=desired,
            original_manager=original_manager,
            notes=(
                "Flatpak instala una rama; la versión exacta puede depender del commit todavía disponible en el remote.",
            ),
        )
    ]


def _parse_snap_info(
    text: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    result: list[RestoreCandidate] = []
    in_channels = False
    for raw in text.splitlines():
        if raw.strip().lower().startswith("channels:"):
            in_channels = True
            continue
        if not in_channels:
            continue
        match = re.match(r"\s*([^:]+/[^:]+):\s+(\S+)\s+\S+\s+\((\d+)\)", raw)
        if not match:
            continue
        channel, version, revision = match.groups()
        result.append(
            _candidate(
                capability=capability,
                manager="snap",
                name=name,
                version=version,
                architecture=architecture,
                source_type="repository",
                source="Snap Store",
                branch=channel,
                revision=revision,
                desired_version=desired,
                original_manager=original_manager,
            )
        )
    return result


def _parse_dnf_list(
    text: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    result: list[RestoreCandidate] = []
    for raw in text.splitlines():
        columns = raw.split()
        if len(columns) < 3 or columns[0].startswith(("Available", "Last", "Error")):
            continue
        package_arch, version, repository = columns[0], columns[1], columns[2]
        package_name, _, arch = package_arch.rpartition(".")
        if package_name != name:
            continue
        result.append(
            _candidate(
                capability=capability,
                manager="rpm",
                name=name,
                version=version,
                architecture=arch or architecture,
                source_type="repository",
                source=repository,
                remote=repository,
                desired_version=desired,
                original_manager=original_manager,
            )
        )
    return result


def _parse_zypper_search(
    text: str,
    name: str,
    desired: str,
    architecture: str,
    capability: str,
    original_manager: str,
) -> list[RestoreCandidate]:
    result: list[RestoreCandidate] = []
    for raw in text.splitlines():
        if "|" not in raw:
            continue
        columns = [column.strip() for column in raw.split("|")]
        # Estado | Nombre | Resumen | Tipo | Versión | Arquitectura | Repositorio
        if len(columns) < 7 or columns[1] != name:
            continue
        version, arch, repository = columns[4], columns[5], columns[6]
        if not version:
            continue
        result.append(
            _candidate(
                capability=capability,
                manager="zypper",
                name=name,
                version=version,
                architecture=arch or architecture,
                source_type="repository",
                source=repository,
                remote=repository,
                desired_version=desired,
                original_manager=original_manager,
            )
        )
    return result


def _colon_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def _version_from_filename(manager: str, name: str, filename: str) -> str:
    if manager == "apt":
        match = re.match(re.escape(name) + r"_([^_]+)_", filename)
        return match.group(1) if match else ""
    if manager == "pacman":
        base = filename.split(".pkg.tar", 1)[0]
        if base.startswith(name + "-"):
            remainder = base[len(name) + 1 :]
            parts = remainder.rsplit("-", 2)
            return parts[0] if parts else ""
    if manager in {"rpm", "zypper"}:
        base = filename[:-4] if filename.endswith(".rpm") else filename
        if base.startswith(name + "-"):
            return base[len(name) + 1 :].rsplit(".", 1)[0]
    return ""


def _repository_refresh_command(candidate: RestoreCandidate, which: Which) -> list[str] | None:
    """Actualiza metadatos antes de asegurar Plasma desde repositorios firmados."""
    if candidate.capability != "desktop.kde-plasma" or candidate.source_type != "repository":
        return None
    manager = candidate.manager.lower()
    if manager == "apt" and which("apt-get"):
        prefix = _with_privilege([], which)
        return apt_update_argv(prefix)
    if manager == "dnf" and which("dnf"):
        return _with_privilege(["dnf", "makecache", "--refresh", "-y"], which)
    if manager == "rpm" and which("dnf"):
        return _with_privilege(["dnf", "makecache", "--refresh", "-y"], which)
    if manager == "zypper" and which("zypper"):
        return _with_privilege(["zypper", "--gpg-auto-import-keys", "refresh"], which)
    if manager == "pacman" and which("pacman"):
        # Arch no admite actualizaciones parciales seguras: -Syu sincroniza y
        # actualiza el sistema antes de asegurar el metapaquete de Plasma.
        return _with_privilege(["pacman", "-Syu", "--noconfirm"], which)
    return None


def _install_command(
    candidate: RestoreCandidate,
    destination_home: str | Path | None,
    which: Which,
) -> tuple[list[str] | None, Path | None]:
    manager = candidate.manager.lower()
    artifact = Path(candidate.artifact_path) if candidate.artifact_path else None

    if manager == "appimage" or (artifact and artifact.suffix.lower() == ".appimage"):
        if not artifact or not artifact.is_file():
            return None, None
        home = Path(destination_home) if destination_home else Path.home()
        return None, home / "Applications" / artifact.name

    if artifact and artifact.is_file():
        if artifact.suffix == ".deb" and which("apt-get"):
            prefix = _with_privilege([], which)
            return apt_install_argv(prefix, str(artifact)), None
        if ".pkg.tar" in artifact.name and which("pacman"):
            return _with_privilege(["pacman", "-U", "--needed", "--noconfirm", str(artifact)], which), None
        if artifact.suffix == ".rpm":
            if which("dnf"):
                return _with_privilege(["dnf", "install", "-y", str(artifact)], which), None
            if which("zypper"):
                return _with_privilege(["zypper", "--non-interactive", "install", str(artifact)], which), None

    if manager == "apt" and which("apt-get"):
        spec = f"{candidate.name}={candidate.version}" if candidate.version else candidate.name
        prefix = _with_privilege([], which)
        return apt_install_argv(prefix, spec), None
    if manager == "pacman" and which("pacman"):
        return _with_privilege(["pacman", "-S", "--needed", "--noconfirm", candidate.name], which), None
    if manager == "rpm" and which("dnf"):
        spec = f"{candidate.name}-{candidate.version}" if candidate.version else candidate.name
        return _with_privilege(["dnf", "install", "--refresh", "-y", spec], which), None
    if manager == "zypper" and which("zypper"):
        spec = f"{candidate.name}={candidate.version}" if candidate.version else candidate.name
        return _with_privilege(["zypper", "--non-interactive", "install", spec], which), None
    if manager == "flatpak" and which("flatpak"):
        target = candidate.name + (f"//{candidate.branch}" if candidate.branch else "")
        argv = ["flatpak", "install", "-y"]
        if candidate.remote:
            argv.append(candidate.remote)
        argv.append(target)
        return argv, None
    if manager == "snap" and which("snap"):
        argv = ["snap", "install", candidate.name]
        if candidate.revision:
            argv.extend(["--revision", candidate.revision])
        elif candidate.branch:
            argv.extend(["--channel", candidate.branch])
        return _with_privilege(argv, which), None
    return None, None


def _with_privilege(argv: list[str], which: Which) -> list[str]:
    """Añade elevación sin intentar leer la contraseña desde la TUI.

    Para acciones iniciadas desde la interfaz se prefiere ``pkexec`` porque
    abre el diálogo gráfico de PolicyKit. ``sudo -n`` queda como respaldo para
    sesiones donde la persona ya ejecutó ``sudo -v``. Antes se elegía sudo en
    primer lugar y una restauración válida terminaba con «a password is
    required» aunque KDE tuviera un agente PolicyKit disponible.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return argv
    if which("pkexec"):
        # pkexec usa un entorno reducido; una ruta absoluta evita depender de
        # cómo el agente de PolicyKit construya PATH.
        if argv and argv[0] == "env":
            return ["pkexec", "/usr/bin/env", *argv[1:]]
        return ["pkexec", *argv]
    if which("sudo"):
        return ["sudo", "-n", *argv]
    return argv


__all__ = [
    "AdvancedRestoreError",
    "AdvancedRestoreSettings",
    "CandidateSearchResult",
    "CommandOutput",
    "InstallResult",
    "RestoreCandidate",
    "candidates_for_application",
    "candidates_for_capability",
    "configure_settings",
    "install_candidate",
    "load_settings",
    "save_settings",
]
