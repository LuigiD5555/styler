"""Almacén de objetos por contenido con verificación obligatoria al leer."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from styler.hashing import hash_file
from styler.validation import ValidationError, validate_checksum

OBJECTS_DIRNAME = "objects"
STYLER_DIR = ".styler"


class ObjectStoreError(Exception):
    pass


class ObjectStore:
    def __init__(self, root: str | Path = ".", styler_dir: str = STYLER_DIR) -> None:
        self.root = Path(root)
        self.base = self.root / styler_dir / OBJECTS_DIRNAME
        self.base.mkdir(parents=True, exist_ok=True)

    def path_for(self, checksum: str) -> Path:
        try:
            validate_checksum(checksum)
        except ValidationError as exc:
            raise ObjectStoreError(str(exc)) from exc
        return self.base / checksum[:2] / checksum

    def has(self, checksum: str) -> bool:
        try:
            return self.path_for(checksum).is_file()
        except ObjectStoreError:
            return False

    def store_file(self, source_path: str | Path) -> tuple[str, str]:
        source = Path(source_path)
        if not source.is_file() or source.is_symlink():
            raise ObjectStoreError(f"No existe, no es archivo regular o es enlace: {source}")
        checksum, _size = hash_file(str(source))
        destination = self.path_for(checksum)
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
            try:
                shutil.copy2(source, temporary)
                actual, _ = hash_file(str(temporary))
                if actual != checksum:
                    raise ObjectStoreError("El contenido cambió mientras se guardaba.")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        elif not self.verify(checksum):
            raise ObjectStoreError(f"El objeto existente está corrupto: {checksum}")
        return checksum, str(destination)

    def store_bytes(self, data: bytes) -> tuple[str, str]:
        checksum = hashlib.blake2b(data, digest_size=16).hexdigest()
        destination = self.path_for(checksum)
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
            try:
                temporary.write_bytes(data)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        elif not self.verify(checksum):
            raise ObjectStoreError(f"El objeto existente está corrupto: {checksum}")
        return checksum, str(destination)

    def restore_file(self, checksum: str, destination: str | Path, mode: int | None = None) -> None:
        """Materializa un objeto verificado mediante reemplazo atómico."""
        source = self.path_for(checksum)
        if not source.is_file():
            raise ObjectStoreError(f"No existe el objeto solicitado: {checksum}")
        if not self.verify(checksum):
            raise ObjectStoreError(f"El objeto está corrupto: {checksum}")
        destination = Path(destination)
        if destination.exists() and destination.is_dir():
            raise ObjectStoreError(f"El destino es un directorio: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".styler-tmp-{os.getpid()}")
        try:
            shutil.copyfile(source, temporary)
            if mode is not None:
                os.chmod(temporary, mode)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            actual, _ = hash_file(str(temporary))
            if actual != checksum:
                raise ObjectStoreError("La copia temporal no coincide con el objeto original.")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def read_bytes(self, checksum: str) -> bytes:
        source = self.path_for(checksum)
        if not source.is_file():
            raise ObjectStoreError(f"No existe el objeto solicitado: {checksum}")
        if not self.verify(checksum):
            raise ObjectStoreError(f"El objeto está corrupto: {checksum}")
        return source.read_bytes()

    def verify(self, checksum: str) -> bool:
        try:
            path = self.path_for(checksum)
        except ObjectStoreError:
            return False
        if not path.is_file() or path.is_symlink():
            return False
        actual, _size = hash_file(str(path))
        return actual == checksum

    def object_count(self) -> int:
        return sum(
            1 for path in self.base.rglob("*")
            if path.is_file() and ".tmp-" not in path.name and ".styler-tmp-" not in path.name
        )
