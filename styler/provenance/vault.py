"""Bóveda local de artefactos conocidos por el inventario de procedencia.

La bóveda solo copia archivos que ya existen. No descarga, no instala y no
necesita privilegios. Sirve para conservar AppImages y paquetes que aún estén
en la caché del gestor antes de que desaparezcan.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from styler.provenance.models import ApplicationRecord, Inventory

VAULT_DIRNAME = "artifacts"
INDEX_NAME = "index.json"


@dataclass(frozen=True)
class VaultResult:
    app_id: str
    status: str  # preserved | already-present | unavailable | failed
    source: str = ""
    destination: str = ""
    checksum: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "app_id": self.app_id,
            "status": self.status,
            "source": self.source,
            "destination": self.destination,
            "checksum": self.checksum,
            "message": self.message,
        }


def vault_dir(root: str | Path = ".") -> Path:
    path = Path(root) / ".styler" / VAULT_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def preserve_inventory_artifacts(
    inventory: Inventory,
    root: str | Path = ".",
    app_ids: set[str] | None = None,
) -> list[VaultResult]:
    directory = vault_dir(root)
    results: list[VaultResult] = []
    index = _load_index(directory)

    for record in inventory.applications:
        if app_ids is not None and record.app_id not in app_ids:
            continue
        result = preserve_record(record, directory)
        results.append(result)
        if result.status in {"preserved", "already-present"}:
            index[record.app_id] = {
                "path": result.destination,
                "checksum": result.checksum,
                "version": record.version,
                "manager": record.manager,
            }
            record.integrity.artifact_path = result.destination
            record.integrity.artifact_available = True
            if not record.integrity.checksum:
                record.integrity.checksum = result.checksum

    _write_index(directory, index)
    return results


def preserve_record(record: ApplicationRecord, directory: Path) -> VaultResult:
    raw = record.integrity.artifact_path
    if not raw:
        return VaultResult(
            record.app_id,
            "unavailable",
            message="El gestor no reportó un archivo local que se pueda conservar.",
        )
    source = Path(raw).expanduser()
    if not source.is_file() or source.is_symlink():
        return VaultResult(
            record.app_id,
            "unavailable",
            source=str(source),
            message="El artefacto ya no existe o es un enlace simbólico.",
        )

    checksum = _sha256(source)
    if not checksum:
        return VaultResult(
            record.app_id,
            "failed",
            source=str(source),
            message="No se pudo calcular la integridad del artefacto.",
        )
    declared = record.integrity.checksum.removeprefix("sha256:")
    if declared and declared != checksum:
        return VaultResult(
            record.app_id,
            "failed",
            source=str(source),
            checksum=f"sha256:{checksum}",
            message="El checksum actual no coincide con el registrado.",
        )

    safe_name = _safe_name(source.name)
    target = directory / f"{checksum[:16]}-{safe_name}"
    if target.is_file() and _sha256(target) == checksum:
        return VaultResult(
            record.app_id,
            "already-present",
            source=str(source),
            destination=str(target),
            checksum=f"sha256:{checksum}",
        )

    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with source.open("rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if _sha256(temporary) != checksum:
            temporary.unlink(missing_ok=True)
            return VaultResult(
                record.app_id,
                "failed",
                source=str(source),
                checksum=f"sha256:{checksum}",
                message="La copia no superó la verificación de integridad.",
            )
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        return VaultResult(
            record.app_id,
            "failed",
            source=str(source),
            checksum=f"sha256:{checksum}",
            message=str(exc),
        )

    return VaultResult(
        record.app_id,
        "preserved",
        source=str(source),
        destination=str(target),
        checksum=f"sha256:{checksum}",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _safe_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in ".-_" else "-" for char in name)
    return cleaned.strip(".-")[:180] or "artifact"


def _load_index(directory: Path) -> dict:
    path = directory / INDEX_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_index(directory: Path, data: dict) -> None:
    path = directory / INDEX_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
