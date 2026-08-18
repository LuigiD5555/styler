"""Hashing compatible y opcionalmente acelerado de Styler.

Orden de backends:
1. extensión PyO3 ``styler_rust``;
2. implementación Python.

Ambos producen BLAKE2b-128, equivalente a ``hashlib.blake2b(digest_size=16)``.
El algoritmo forma parte del formato persistente del object store y no debe
cambiar sin migración explícita.
"""
from __future__ import annotations

import hashlib
import os
from typing import Iterable

from styler.models import FileEntry

try:
    import styler_rust as _rust  # type: ignore
except ImportError:
    _rust = None
else:
    if getattr(_rust, "HASH_ALGORITHM", "") != "blake2b-128":
        _rust = None


def active_backend() -> str:
    return "rust-extension" if _rust is not None else "python"


def _hash_file_python(path: str, chunk_size: int = 1 << 20) -> tuple[str, int]:
    h = hashlib.blake2b(digest_size=16)
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _hash_tree_python(paths: Iterable[str]) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for root in paths:
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            walk_targets = [(os.path.dirname(root), [], [os.path.basename(root)])]
        else:
            walk_targets = os.walk(root, followlinks=False)
        for dirpath, _dirnames, filenames in walk_targets:
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    checksum, size = _hash_file_python(full)
                except (OSError, PermissionError):
                    continue
                entries.append(FileEntry(path=full, checksum=checksum, size=size))
    entries.sort(key=lambda entry: entry.path)
    return entries


def hash_file(path: str) -> tuple[str, int]:
    """Calcula BLAKE2b-128 de un archivo sin cambiar el contrato histórico."""
    if _rust is not None and hasattr(_rust, "hash_file"):
        result = _rust.hash_file(path)
        if result is not None:
            checksum, size = result
            return str(checksum), int(size)
    return _hash_file_python(path)


def hash_tree(paths: Iterable[str]) -> list[FileEntry]:
    """Calcula checksums de archivos/directorios sin seguir symlinks."""
    normalized = [os.fspath(path) for path in paths]
    if _rust is not None:
        raw = _rust.hash_tree(normalized)
        return [FileEntry(path=p, checksum=c, size=s) for p, c, s in raw]
    return _hash_tree_python(normalized)
