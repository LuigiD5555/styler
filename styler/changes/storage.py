"""Persistencia durable del estado de Cambios.

Este módulo concentra únicamente I/O de estado. No conoce planes, DAG, UI ni
PipeCraft; así `ChangeService` no mezcla reglas de negocio con detalles de
escritura atómica, montajes y errores de filesystem.
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from typing import Any


class ChangeStateWriteError(OSError):
    """Styler no pudo persistir el estado del cambio con seguridad."""

    def __init__(self, path: Path, original: OSError) -> None:
        self.path = Path(path)
        self.original = original
        super().__init__(getattr(original, "errno", None), str(original), str(path))


def mount_status(path: Path) -> str:
    """Describe el montaje que contiene *path* sin depender de comandos externos."""
    try:
        candidate = path.resolve(strict=False)
        best: tuple[int, str, str] | None = None
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            left, _sep, right = line.partition(" - ")
            fields = left.split()
            if len(fields) < 6:
                continue
            mountpoint = Path(fields[4].replace("\\040", " "))
            options = fields[5]
            try:
                candidate.relative_to(mountpoint)
            except ValueError:
                continue
            score = len(str(mountpoint))
            fs_type = right.split()[0] if right else "?"
            if best is None or score > best[0]:
                best = (score, str(mountpoint), f"{options}; fs={fs_type}")
        if best is not None:
            return f"montaje={best[1]} · {best[2]}"
    except OSError:
        pass
    return "montaje no disponible"


def storage_error(exc: OSError, fallback: Path) -> ChangeStateWriteError:
    """Conserva errno/path del error real aunque venga envuelto por recibos."""
    current: BaseException | None = exc
    chosen: OSError = exc
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError):
            chosen = current
            if getattr(current, "errno", None) in {errno.EROFS, errno.EACCES, errno.EPERM}:
                break
        current = current.__cause__ or current.__context__
    filename = getattr(chosen, "filename", None)
    return ChangeStateWriteError(Path(filename) if filename else fallback, chosen)


def is_storage_failure(exc: OSError) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and getattr(current, "errno", None) in {
            errno.EROFS,
            errno.EACCES,
            errno.EPERM,
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def probe_directory_writable(path: Path) -> None:
    """Prueba una escritura real y atómica, no sólo ``os.access``."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".styler-write-probe-{os.getpid()}-{time.time_ns()}"
        with probe.open("x", encoding="utf-8") as handle:
            handle.write("styler write probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError as exc:
        try:
            if "probe" in locals():
                probe.unlink(missing_ok=True)
        except OSError:
            pass
        raise ChangeStateWriteError(path, exc) from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Escritura atómica con fsync del archivo temporal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_record(path: Path, change_id: str, values: dict[str, Any]) -> None:
    records = read_json(path)
    previous = dict(records.get(change_id, {}))
    previous.update(values)
    previous["updated_at"] = time.time()
    records[change_id] = previous
    try:
        write_json(path, records)
    except OSError as exc:
        raise ChangeStateWriteError(path, exc) from exc
