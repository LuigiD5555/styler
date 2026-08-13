"""
styler.provenance.detectors.appimage
====================================
Un AppImage no tiene gestor de paquetes: es un archivo suelto. Por eso es el
caso más frágil y el que más necesita procedencia.

Styler NO ejecuta el AppImage para preguntarle nada. Lee la sección ELF
``.upd_info``, que es donde el propio archivo declara de dónde se actualiza
(``gh-releases-zsync|owner|repo|tag|patrón.zsync``). Es evidencia declarada por
el binario, no una suposición por parecido de nombre.

También registra:

* checksum SHA-256 del archivo;
* el ``.zsync`` de al lado, si existe;
* que el artefacto SÍ está disponible localmente (el archivo es la aplicación).
"""
from __future__ import annotations

import hashlib
import os
import struct
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
from styler.provenance.upstream import upstream_from_update_information

DEFAULT_DIRS = (
    "~/Applications",
    "~/.local/bin",
    "~/AppImages",
    "~/Apps",
    "~/bin",
    "/opt",
)

MAX_SECTION = 4096


class AppImageDetector(Detector):
    name = "appimage"
    manager = "appimage"

    def __init__(
        self,
        runner: Runner | None = None,
        search_dirs: list[str | Path] | None = None,
        max_depth: int = 4,
        max_files: int = 20_000,
    ) -> None:
        super().__init__(runner)
        raw = search_dirs if search_dirs is not None else list(DEFAULT_DIRS)
        self.search_dirs = [Path(str(p)).expanduser() for p in raw]
        self.max_depth = max(0, max_depth)
        self.max_files = max(1, max_files)

    def applies(self) -> bool:
        return any(directory.is_dir() for directory in self.search_dirs)

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        records: list[ApplicationRecord] = []
        for path in self._find_appimages():
            records.append(self._record(path))
        return records

    def _find_appimages(self) -> list[Path]:
        """Busca de forma recursiva, acotada y sin seguir enlaces simbólicos."""
        found: list[Path] = []
        seen: set[str] = set()
        visited_files = 0
        ignored_dirs = {".git", ".cache", "node_modules", "__pycache__", ".venv", "venv"}
        for directory in self.search_dirs:
            if not directory.is_dir():
                continue
            base_depth = len(directory.resolve().parts)
            for root, dirs, files in os.walk(directory, followlinks=False):
                root_path = Path(root)
                depth = len(root_path.resolve().parts) - base_depth
                dirs[:] = [
                    name for name in dirs
                    if name not in ignored_dirs
                    and not (root_path / name).is_symlink()
                    and depth < self.max_depth
                ]
                for name in sorted(files):
                    visited_files += 1
                    if visited_files > self.max_files:
                        self.problems.append(
                            f"appimage: búsqueda detenida al alcanzar {self.max_files} archivos"
                        )
                        return found
                    path = root_path / name
                    if path.is_symlink() or path.suffix.lower() != ".appimage":
                        continue
                    try:
                        resolved = str(path.resolve())
                    except OSError:
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    found.append(path)
        return sorted(found)

    def _record(self, path: Path) -> ApplicationRecord:
        warnings: list[str] = []
        update_info = read_update_information(path)
        checksum = sha256_of(path)
        zsync = path.with_name(path.name + ".zsync")

        upstream = upstream_from_update_information(
            update_info, evidence=f"sección ELF .upd_info de {path.name}"
        )
        if not update_info:
            warnings.append(
                "Este AppImage no declara de dónde se actualiza: no hay forma "
                "automática de saber si existe una versión más reciente."
            )

        origin = Origin(
            kind=OriginKind.APPIMAGE,
            remote_name=upstream.repository or "",
            remote_url=upstream.url or "",
            vendor="",
            signed=None,
            confidence=(
                Confidence.CONFIRMED if upstream.confidence == Confidence.CONFIRMED
                else Confidence.UNKNOWN
            ),
            evidence="archivo AppImage local, .upd_info",
        )

        name, version = split_name_version(path.stem)

        return ApplicationRecord(
            app_id=f"appimage:{name}",
            name=name,
            display_name=name,
            manager="appimage",
            version=version,
            architecture="",
            install_method="appimage",
            install_reason=InstallReason.PORTABLE,
            origin=origin,
            upstream=upstream,
            integrity=Integrity(
                checksum=f"sha256:{checksum}" if checksum else "",
                signature_verified=None,
                artifact_path=str(path),
                artifact_available=True,
            ),
            warnings=warnings + (
                [] if zsync.is_file() else []
            ),
        )


# -- utilidades puras -----------------------------------------------------


def sha256_of(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def split_name_version(stem: str) -> tuple[str, str]:
    """Separa 'Krita-5.2.2-x86_64' en ('Krita', '5.2.2') sin inventar datos."""
    pieces = stem.split("-")
    name = pieces[0] if pieces else stem
    version = ""
    for piece in pieces[1:]:
        candidate = piece.lstrip("vV")
        if candidate and candidate[0].isdigit():
            version = candidate
            break
    return name or stem, version


def read_update_information(path: str | Path) -> str:
    """Lee la sección ELF ``.upd_info`` sin ejecutar el archivo."""
    try:
        with open(path, "rb") as handle:
            data = _read_section(handle, ".upd_info")
    except OSError:
        return ""
    if not data:
        return ""
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _read_section(handle, wanted: str) -> bytes:
    header = handle.read(64)
    if len(header) < 64 or header[:4] != b"\x7fELF":
        return b""
    bits = header[4]          # 1 = 32 bits, 2 = 64 bits
    endian = "<" if header[5] == 1 else ">"

    if bits == 2:
        e_shoff = struct.unpack_from(endian + "Q", header, 0x28)[0]
        e_shentsize = struct.unpack_from(endian + "H", header, 0x3A)[0]
        e_shnum = struct.unpack_from(endian + "H", header, 0x3C)[0]
        e_shstrndx = struct.unpack_from(endian + "H", header, 0x3E)[0]
    elif bits == 1:
        e_shoff = struct.unpack_from(endian + "I", header, 0x20)[0]
        e_shentsize = struct.unpack_from(endian + "H", header, 0x2E)[0]
        e_shnum = struct.unpack_from(endian + "H", header, 0x30)[0]
        e_shstrndx = struct.unpack_from(endian + "H", header, 0x32)[0]
    else:
        return b""

    if not e_shoff or not e_shnum or e_shstrndx >= e_shnum:
        return b""

    handle.seek(e_shoff)
    table = handle.read(e_shentsize * e_shnum)
    if len(table) < e_shentsize * e_shnum:
        return b""

    def entry(index: int) -> tuple[int, int, int]:
        base = index * e_shentsize
        if bits == 2:
            sh_name = struct.unpack_from(endian + "I", table, base)[0]
            sh_offset = struct.unpack_from(endian + "Q", table, base + 0x18)[0]
            sh_size = struct.unpack_from(endian + "Q", table, base + 0x20)[0]
        else:
            sh_name = struct.unpack_from(endian + "I", table, base)[0]
            sh_offset = struct.unpack_from(endian + "I", table, base + 0x10)[0]
            sh_size = struct.unpack_from(endian + "I", table, base + 0x14)[0]
        return sh_name, sh_offset, sh_size

    _, str_offset, str_size = entry(e_shstrndx)
    handle.seek(str_offset)
    names = handle.read(str_size)

    for index in range(e_shnum):
        sh_name, sh_offset, sh_size = entry(index)
        end = names.find(b"\x00", sh_name)
        section_name = names[sh_name:end].decode("ascii", errors="replace")
        if section_name != wanted:
            continue
        if sh_size == 0 or sh_size > MAX_SECTION:
            return b""
        handle.seek(sh_offset)
        return handle.read(sh_size)
    return b""
