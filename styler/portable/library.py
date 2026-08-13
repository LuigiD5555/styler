"""Biblioteca persistente de paquetes portables."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable

from styler.paths import ensure_library_root

from .models import ArtifactEntry, InstalledPackage, PackageManifest, PackageType, PortablePackageError
from .package import CHECKSUMS_NAME, MANIFEST_NAME, file_sha256, inspect_package

PACKAGES_DIR = "portable-packages"
INSTALL_RECORD = "installed.json"
ARCHIVE_COPY = "package.stylerpkg"


class PortableLibrary:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = ensure_library_root(root)
        self.packages_root = self.root / PACKAGES_DIR
        self.packages_root.mkdir(parents=True, exist_ok=True)

    def identities(self) -> tuple[str, ...]:
        return tuple(package.identity for package in self.list_packages())

    def inspect(self, source: str | Path):
        return inspect_package(source, installed_identities=self.identities())

    def import_package(
        self,
        source: str | Path,
        *,
        collision_policy: str = "reject",
    ) -> InstalledPackage:
        if collision_policy not in {"reject", "replace_explicitly"}:
            raise PortablePackageError(
                "collision_policy debe ser 'reject' o 'replace_explicitly'."
            )

        # Primero se copia a staging y se valida ESA copia. Así el archivo no
        # puede cambiar entre inspección y extracción (TOCTOU).
        staging = Path(tempfile.mkdtemp(prefix=".stylerpkg-", dir=self.packages_root))
        staged_archive = staging / ARCHIVE_COPY
        try:
            shutil.copy2(source, staged_archive)
            inspection = inspect_package(
                staged_archive, installed_identities=self.identities()
            )
            manifest = inspection.manifest
            if manifest.package_type is PackageType.BASELINE:
                raise PortablePackageError(
                    "Las líneas base se importan desde Constructor de cambios, no en la biblioteca de cambios."
                )
            destination = self._version_dir(manifest.package_id, manifest.version)
            if destination.exists() and collision_policy == "reject":
                raise PortablePackageError(
                    f"Ya está instalado {manifest.package_id}@{manifest.version}. "
                    "Usa reemplazo explícito para sustituirlo."
                )

            content = staging / "content"
            content.mkdir()
            with zipfile.ZipFile(staged_archive) as archive:
                for info in archive.infolist():
                    target = content / info.filename
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source_handle, target.open("wb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle)
            record = {
                "manifest": manifest.to_dict(),
                "imported_at": time.time(),
                "source_checksum": file_sha256(staged_archive),
            }
            (staging / INSTALL_RECORD).write_text(
                json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            replacement_backup: Path | None = None
            if destination.exists():
                replacement_backup = Path(
                    tempfile.mkdtemp(prefix=f".{destination.name}-previous-", dir=destination.parent)
                )
                replacement_backup.rmdir()
                os.replace(destination, replacement_backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if replacement_backup is not None and replacement_backup.exists():
                    os.replace(replacement_backup, destination)
                raise
            else:
                if replacement_backup is not None and replacement_backup.exists():
                    shutil.rmtree(replacement_backup, ignore_errors=True)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return self.get(manifest.package_id, manifest.version)

    def list_packages(self) -> tuple[InstalledPackage, ...]:
        packages: list[InstalledPackage] = []
        if not self.packages_root.is_dir():
            return ()
        for record_path in self.packages_root.glob("*/*/installed.json"):
            try:
                package = self._load_record(record_path.parent)
            except (OSError, ValueError, PortablePackageError, json.JSONDecodeError):
                continue
            packages.append(package)
        return tuple(
            sorted(
                packages,
                key=lambda item: (item.manifest.package_id, item.manifest.version),
            )
        )

    def get(self, package_id: str, version: str | None = None) -> InstalledPackage:
        if version:
            path = self._version_dir(package_id, version)
            if not path.is_dir():
                raise PortablePackageError(f"No está instalado {package_id}@{version}.")
            return self._load_record(path)
        matches = [item for item in self.list_packages() if item.manifest.package_id == package_id]
        if not matches:
            raise PortablePackageError(f"No está instalado el paquete '{package_id}'.")
        return sorted(matches, key=lambda item: _version_key(item.manifest.version), reverse=True)[0]

    def remove(self, package_id: str, version: str | None = None) -> None:
        package = self.get(package_id, version)
        path = Path(package.install_path)
        shutil.rmtree(path)
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def remove_all(self, package_id: str) -> None:
        """Elimina todas las versiones locales de un paquete.

        Un cambio disponible no debe reaparecer desde una versión vieja después
        de borrarlo desde la pestaña Cambios.
        """
        matches = [item for item in self.list_packages() if item.manifest.package_id == package_id]
        if not matches:
            raise PortablePackageError(f"No está instalado el paquete '{package_id}'.")
        for package in matches:
            path = Path(package.install_path)
            if path.exists():
                shutil.rmtree(path)
        parent = self.packages_root / package_id
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def export(self, package_id: str, destination: str | Path, version: str | None = None) -> Path:
        package = self.get(package_id, version)
        source = Path(package.install_path) / ARCHIVE_COPY
        destination = Path(destination)
        if destination.is_dir():
            safe_id = package.manifest.package_id.replace("/", "-")
            destination = destination / f"{safe_id}-{package.manifest.version}.stylerpkg"
        if destination.suffix != ".stylerpkg":
            destination = destination.with_suffix(".stylerpkg")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    def artifacts(
        self,
        *,
        kind: str | None = None,
    ) -> tuple[tuple[InstalledPackage, ArtifactEntry], ...]:
        result: list[tuple[InstalledPackage, ArtifactEntry]] = []
        for package in self.list_packages():
            for artifact in package.manifest.artifacts:
                if kind is None or artifact.kind == kind:
                    result.append((package, artifact))
        return tuple(result)

    def find_artifact(self, kind: str, artifact_id: str) -> tuple[InstalledPackage, ArtifactEntry]:
        matches = [item for item in self.artifacts(kind=kind) if item[1].artifact_id == artifact_id]
        if not matches:
            raise PortablePackageError(f"No existe {kind}:{artifact_id} en paquetes registrados.")
        if len(matches) > 1:
            identities = ", ".join(package.identity for package, _ in matches)
            raise PortablePackageError(
                f"El artefacto {kind}:{artifact_id} es ambiguo; aparece en {identities}."
            )
        return matches[0]

    def read_artifact(self, package: InstalledPackage, artifact: ArtifactEntry) -> bytes:
        path = Path(package.install_path) / "content" / artifact.path
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PortablePackageError(f"No se pudo leer {artifact.path}: {exc}") from exc

    def component_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for package in self.list_packages():
            if any(item.kind == "component" for item in package.manifest.artifacts):
                root = Path(package.install_path) / "component-catalog"
                self._materialize_component_catalog(package, root)
                roots.append(root)
        return tuple(roots)

    def _materialize_component_catalog(self, package: InstalledPackage, root: Path) -> None:
        content = Path(package.install_path) / "content"
        root.mkdir(parents=True, exist_ok=True)
        index: dict[str, str] = {}
        for artifact in package.manifest.artifacts:
            if artifact.kind not in {"component", "asset"}:
                continue
            source = content / artifact.path
            if artifact.kind == "component":
                relative = artifact.path
                index[artifact.artifact_id] = relative
            else:
                relative = artifact.path
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or source.stat().st_mtime_ns != target.stat().st_mtime_ns:
                shutil.copy2(source, target)
        lines = ["[components]"]
        for component_id, relative in sorted(index.items()):
            lines.append(f'{json.dumps(component_id)} = {json.dumps(relative)}')
        (root / "index.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_record(self, path: Path) -> InstalledPackage:
        raw = json.loads((path / INSTALL_RECORD).read_text(encoding="utf-8"))
        manifest = PackageManifest.from_dict(raw["manifest"])
        return InstalledPackage(
            manifest=manifest,
            install_path=str(path),
            imported_at=float(raw.get("imported_at", 0.0)),
            source_checksum=str(raw.get("source_checksum", "")),
        )

    def _version_dir(self, package_id: str, version: str) -> Path:
        return self.packages_root / package_id / version


def _version_key(version: str) -> tuple:
    parts: list[object] = []
    for token in version.replace("-", ".").replace("+", ".").split("."):
        parts.append(int(token) if token.isdigit() else token)
    return tuple(parts)
