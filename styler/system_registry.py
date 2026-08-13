"""Registro automático y privado del estado del sistema.

Esta capa responde a una necesidad distinta de una captura de perfil: conserva
un inventario persistente de aplicaciones, paquetes, herramientas, repositorios,
servicios y configuración visual conocida desde que Styler se instala.

Privacidad por diseño
---------------------
* Nunca recorre ``$HOME`` completo.
* Nunca entra en Documentos, Descargas, Imágenes, Vídeos, Música, proyectos,
  perfiles de navegador, SSH, GPG, llaveros o historiales.
* Los archivos permitidos se registran por ruta, tamaño, modo y checksum. Su
  contenido no se copia al registro.
* Los lanzadores ``.desktop`` solo exponen claves públicas necesarias para
  reconocer una aplicación o personalización.

El registro es un índice. La exportación de contenido sigue siendo una decisión
posterior y explícita del usuario.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit

from styler.provenance import inventory as provenance_inventory
from styler.provenance.models import ApplicationRecord, Inventory
from styler.runtime.commands import PipeCraftRunner

REGISTRY_SCHEMA = "styler.system-registry/1"
EVENT_SCHEMA = "styler.system-registry-event/1"
REGISTRY_DIRNAME = "registry"
SNAPSHOTS_DIRNAME = "snapshots"
CURRENT_POINTER = "current.json"
BASELINE_POINTER = "baseline.json"
INSTALL_PRE_POINTER = "install-pre.json"
INSTALL_POST_POINTER = "install-post.json"
EVENTS_FILENAME = "events.jsonl"
LAST_ERROR_FILENAME = "last-error.json"

ProgressHook = Callable[[str, int, int], None] | None

# Solo se exploran estas rutas del usuario. No hay búsqueda libre bajo HOME.
_SAFE_EXACT_USER_FILES = (
    ".config/kdeglobals",
    ".config/kcminputrc",
    ".config/kglobalshortcutsrc",
    ".config/kwinrc",
    ".config/plasmarc",
    ".config/plasmashellrc",
    ".config/plasma-org.kde.plasma.desktop-appletsrc",
    ".config/konsolerc",
    ".config/dolphinrc",
    ".config/gtk-3.0/settings.ini",
    ".config/gtk-4.0/settings.ini",
    ".config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml",
    ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml",
    ".config/cinnamon/spices.cache",
    ".config/mimeapps.list",
    ".local/share/applications/mimeapps.list",
)

# Directorios de recursos/configuración pública. Cada entrada limita profundidad,
# cantidad y extensiones para evitar convertir el registro en un crawler de HOME.
_SAFE_USER_TREES: tuple[tuple[str, int, int, frozenset[str]], ...] = (
    (".local/share/applications", 1, 2_000, frozenset({".desktop"})),
    (".local/share/icons", 3, 8_000, frozenset({".theme", ".index", ".png", ".svg", ".xpm"})),
    (".local/share/themes", 4, 8_000, frozenset({".css", ".scss", ".ini", ".xml", ".conf", ".png", ".svg"})),
    (".local/share/color-schemes", 2, 2_000, frozenset({".colors"})),
    (".local/share/plasma/look-and-feel", 5, 8_000, frozenset({".json", ".xml", ".conf", ".qml", ".js", ".svg", ".png"})),
    (".local/share/plasma/desktoptheme", 4, 8_000, frozenset({".svg", ".svgz", ".png", ".json", ".metadata", ".desktop"})),
    (".icons", 3, 8_000, frozenset({".theme", ".index", ".png", ".svg", ".xpm"})),
    (".themes", 4, 8_000, frozenset({".css", ".scss", ".ini", ".xml", ".conf", ".png", ".svg"})),
    # PhotoGIMP/GIMP: solo configuración y recursos reproducibles, nunca documentos,
    # recientes, sesión, miniaturas ni cachés.
    (".config/GIMP", 6, 20_000, frozenset({"", ".rc", ".conf", ".json", ".xml", ".py", ".scm", ".desktop", ".png", ".svg"})),
    (".var/app/org.gimp.GIMP/config/GIMP", 6, 20_000, frozenset({"", ".rc", ".conf", ".json", ".xml", ".py", ".scm", ".desktop", ".png", ".svg"})),
)

_BLOCKED_PARTS = frozenset(
    {
        ".cache",
        "cache",
        "caches",
        "tmp",
        "temp",
        "thumbnails",
        "recently-used.xbel",
        "recent",
        "recents",
        "history",
        "histories",
        "session",
        "sessions",
        "sessionrc",
        "documents",
        "documentos",
        "downloads",
        "descargas",
        "pictures",
        "imágenes",
        "imagenes",
        "videos",
        "vídeos",
        "music",
        "música",
        "projects",
        "proyectos",
        ".git",
        ".ssh",
        ".gnupg",
        ".password-store",
        "keyrings",
        "cookies",
        "credentials",
        "secrets",
        "tokens",
        "browser",
        "browsers",
        "mozilla",
        "chromium",
        "google-chrome",
    }
)

_BLOCKED_SUFFIXES = frozenset({".log", ".sqlite", ".sqlite3", ".db", ".key", ".pem", ".p12", ".pfx"})

_SAFE_SYSTEM_FILES = (
    "/etc/os-release",
    "/etc/environment",
    "/etc/default/grub",
    "/etc/pacman.conf",
    "/etc/dnf/dnf.conf",
    "/etc/zypp/zypp.conf",
    "/etc/apt/sources.list",
    "/etc/sddm.conf",
    "/etc/gdm/custom.conf",
    "/etc/lightdm/lightdm.conf",
)

_DESKTOP_PUBLIC_KEYS = frozenset(
    {
        "Type",
        "Name",
        "GenericName",
        "Comment",
        "Icon",
        "Exec",
        "TryExec",
        "StartupWMClass",
        "Categories",
        "MimeType",
        "NoDisplay",
        "Hidden",
        "Terminal",
        "X-Flatpak",
        "X-SnapInstanceName",
    }
)


@dataclass(frozen=True)
class RegistryItem:
    item_id: str
    kind: str
    name: str
    source: str
    version: str = ""
    manager: str = ""
    path: str = ""
    checksum: str = ""
    size: int = 0
    mode: str = ""
    exportable: bool = False
    confidence: str = "confirmed"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "manager": self.manager,
            "path": self.path,
            "checksum": self.checksum,
            "size": self.size,
            "mode": self.mode,
            "exportable": self.exportable,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RegistryItem":
        return RegistryItem(
            item_id=str(data["item_id"]),
            kind=str(data.get("kind", "unknown")),
            name=str(data.get("name", "")),
            source=str(data.get("source", "")),
            version=str(data.get("version", "")),
            manager=str(data.get("manager", "")),
            path=str(data.get("path", "")),
            checksum=str(data.get("checksum", "")),
            size=int(data.get("size", 0) or 0),
            mode=str(data.get("mode", "")),
            exportable=bool(data.get("exportable", False)),
            confidence=str(data.get("confidence", "confirmed")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class RegistrySnapshot:
    snapshot_id: str
    captured_at: float
    phase: str
    system_only: bool
    system: dict[str, Any] = field(default_factory=dict)
    managers_seen: list[str] = field(default_factory=list)
    items: list[RegistryItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REGISTRY_SCHEMA,
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "phase": self.phase,
            "system_only": self.system_only,
            "system": dict(self.system),
            "managers_seen": list(self.managers_seen),
            "items": [item.to_dict() for item in self.items],
            "problems": list(self.problems),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RegistrySnapshot":
        if data.get("schema") != REGISTRY_SCHEMA:
            raise ValueError(f"Esquema de registro no soportado: {data.get('schema')}")
        return RegistrySnapshot(
            snapshot_id=str(data["snapshot_id"]),
            captured_at=float(data.get("captured_at", time.time())),
            phase=str(data.get("phase", "manual")),
            system_only=bool(data.get("system_only", False)),
            system=dict(data.get("system", {})),
            managers_seen=[str(value) for value in data.get("managers_seen", [])],
            items=[RegistryItem.from_dict(item) for item in data.get("items", [])],
            problems=[str(value) for value in data.get("problems", [])],
        )

    def by_id(self) -> dict[str, RegistryItem]:
        return {item.item_id: item for item in self.items}

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.items:
            result[item.kind] = result.get(item.kind, 0) + 1
        return dict(sorted(result.items()))


@dataclass(frozen=True)
class RegistryEvent:
    event_id: str
    occurred_at: float
    session: str
    change: str
    item_id: str
    kind: str
    name: str
    exportable: bool
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "session": self.session,
            "change": self.change,
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.name,
            "exportable": self.exportable,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class RegistryUpdate:
    snapshot: RegistrySnapshot
    events: tuple[RegistryEvent, ...]
    baseline_created: bool
    snapshot_path: str


class RegistryError(RuntimeError):
    pass


def registry_dir(root: str | Path) -> Path:
    path = Path(root).expanduser() / REGISTRY_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    (path / SNAPSHOTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return path


def _portable_path(path: Path, home: Path | None) -> str:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser().absolute()
    if home is not None:
        try:
            relative = resolved.relative_to(home.resolve(strict=False))
            return "${HOME}/" + relative.as_posix()
        except (ValueError, OSError):
            pass
    return resolved.as_posix()


def _digest(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.blake2b(digest_size=16)
    try:
        with path.open("rb") as handle:
            while block := handle.read(chunk_size):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _safe_file(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    # Un HOME de pruebas o contenedor puede vivir bajo /tmp. Solo se rechaza
    # tmp/temp cuando aparece como subdirectorio durante el recorrido acotado.
    sensitive_parts = _BLOCKED_PARTS - {"tmp", "temp"}
    if lowered_parts & sensitive_parts:
        return False
    lowered_name = path.name.lower()
    if any(token in lowered_name for token in ("password", "passwd", "secret", "token", "credential", "cookie")):
        return False
    return path.suffix.lower() not in _BLOCKED_SUFFIXES


def _desktop_metadata(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw or raw.startswith(("#", "[")) or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key in _DESKTOP_PUBLIC_KEYS:
                values[key] = value[:2_000]
    except OSError:
        return {}
    return values


def _file_item(path: Path, *, home: Path | None, kind: str, source: str, exportable: bool) -> RegistryItem | None:
    if not path.is_file() or path.is_symlink() or not _safe_file(path):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    portable = _portable_path(path, home)
    metadata: dict[str, Any] = {"mtime_ns": stat.st_mtime_ns}
    if path.suffix.lower() == ".desktop":
        metadata["desktop"] = _desktop_metadata(path)
    return RegistryItem(
        item_id=f"file:{portable}",
        kind=kind,
        name=path.name,
        source=source,
        path=portable,
        checksum=_digest(path),
        size=stat.st_size,
        mode=oct(stat.st_mode & 0o7777),
        exportable=exportable,
        metadata=metadata,
    )


def _iter_limited_tree(base: Path, *, max_depth: int, max_files: int, suffixes: frozenset[str]) -> Iterable[Path]:
    if not base.is_dir() or base.is_symlink():
        return
    try:
        base_depth = len(base.resolve().parts)
    except OSError:
        return
    visited = 0
    for raw_root, dirs, files in os.walk(base, followlinks=False):
        root = Path(raw_root)
        try:
            depth = len(root.resolve().parts) - base_depth
        except OSError:
            dirs[:] = []
            continue
        dirs[:] = [
            name
            for name in sorted(dirs)
            if depth < max_depth
            and name.lower() not in _BLOCKED_PARTS
            and not (root / name).is_symlink()
        ]
        for name in sorted(files):
            visited += 1
            if visited > max_files:
                return
            path = root / name
            suffix = path.suffix.lower()
            if suffixes and suffix not in suffixes:
                continue
            if _safe_file(path):
                yield path


def _user_configuration_items(home: Path) -> tuple[list[RegistryItem], list[str]]:
    items: list[RegistryItem] = []
    problems: list[str] = []
    seen: set[str] = set()

    for relative in _SAFE_EXACT_USER_FILES:
        item = _file_item(
            home / relative,
            home=home,
            kind="visual_configuration",
            source="allowlisted-user-configuration",
            exportable=True,
        )
        if item and item.item_id not in seen:
            seen.add(item.item_id)
            items.append(item)

    for relative, depth, limit, suffixes in _SAFE_USER_TREES:
        base = home / relative
        count = 0
        for path in _iter_limited_tree(base, max_depth=depth, max_files=limit, suffixes=suffixes):
            item = _file_item(
                path,
                home=home,
                kind=("application_launcher" if path.suffix.lower() == ".desktop" else "visual_resource"),
                source="allowlisted-user-tree",
                exportable=True,
            )
            if item and item.item_id not in seen:
                seen.add(item.item_id)
                items.append(item)
                count += 1
        if base.is_dir() and count >= limit:
            problems.append(f"Se alcanzó el límite de {limit} archivos en {_portable_path(base, home)}.")

    items.extend(_detect_photogimp(items, home))
    return items, problems


def _detect_photogimp(items: Sequence[RegistryItem], home: Path) -> list[RegistryItem]:
    evidence: list[str] = []
    config_versions: set[str] = set()
    for item in items:
        desktop = item.metadata.get("desktop", {}) if isinstance(item.metadata, dict) else {}
        values = " ".join(str(value) for value in desktop.values()).lower()
        if "photogimp" in values:
            evidence.append(item.path)
        match = re.search(r"/GIMP/(\d+(?:\.\d+)+)/", item.path)
        if match:
            config_versions.add(match.group(1))
        if "photogimp" in item.path.lower():
            evidence.append(item.path)
    marker_candidates = (
        home / ".var/app/org.gimp.GIMP/config/GIMP/.photogimp-marker",
        home / ".config/GIMP/.photogimp-marker",
    )
    for marker in marker_candidates:
        if marker.is_file():
            evidence.append(_portable_path(marker, home))
    if not evidence:
        return []
    return [
        RegistryItem(
            item_id="customization:photogimp",
            kind="application_customization",
            name="PhotoGIMP",
            source="semantic-detection",
            version=max(config_versions, default=""),
            exportable=True,
            confidence="confirmed" if any("photogimp" in value.lower() for value in evidence) else "inferred",
            metadata={
                "requires": ["flatpak:org.gimp.GIMP", "apt:gimp", "rpm:gimp", "pacman:gimp"],
                "evidence": sorted(set(evidence))[:100],
                "config_versions": sorted(config_versions),
            },
        )
    ]


def _system_configuration_items() -> list[RegistryItem]:
    items: list[RegistryItem] = []
    for raw in _SAFE_SYSTEM_FILES:
        path = Path(raw)
        item = _file_item(
            path,
            home=None,
            kind="system_configuration",
            source="allowlisted-system-configuration",
            exportable=False,
        )
        if item:
            items.append(item)
    return items


def _run(argv: Sequence[str], *, timeout: int = 20) -> tuple[str, str]:
    if not argv or not shutil.which(argv[0]):
        return "", ""
    completed = PipeCraftRunner(timeout=timeout).run(
        list(argv), timeout=timeout, env={**os.environ, "LC_ALL": "C"}
    )
    if completed.returncode != 0 and not completed.stdout:
        return "", completed.stderr.strip()[:500]
    return completed.stdout, ""


def _tool_items(home: Path | None) -> list[RegistryItem]:
    tools = ("python3", "python", "conda", "mamba", "micromamba", "pipx", "uv")
    items: list[RegistryItem] = []
    for name in tools:
        executable = shutil.which(name)
        if not executable:
            continue
        out, _error = _run([executable, "--version"], timeout=5)
        version = (out.strip().splitlines() or [""])[0][:200]
        path = Path(executable)
        items.append(
            RegistryItem(
                item_id=f"tool:{name}:{path.resolve(strict=False)}",
                kind="runtime_tool",
                name=name,
                source="command-discovery",
                version=version,
                path=_portable_path(path, home),
                exportable=name in {"conda", "mamba", "micromamba", "pipx", "uv"},
                metadata={"executable": _portable_path(path, home)},
            )
        )

    if home is not None:
        for display, relative in (
            ("Miniconda", "miniconda3"),
            ("Anaconda", "anaconda3"),
            ("Miniforge", "miniforge3"),
            ("Mambaforge", "mambaforge"),
        ):
            root = home / relative
            if not root.is_dir():
                continue
            history = root / "conda-meta/history"
            checksum = _digest(history) if history.is_file() else ""
            items.append(
                RegistryItem(
                    item_id=f"runtime:{display.lower()}:{_portable_path(root, home)}",
                    kind="runtime_distribution",
                    name=display,
                    source="known-installation-root",
                    path=_portable_path(root, home),
                    checksum=checksum,
                    exportable=True,
                    metadata={"environment_names_scanned": False},
                )
            )
    return items


def _package_items(scope: str, *, progress: ProgressHook = None) -> tuple[list[RegistryItem], Inventory, list[str]]:
    inventory, problems = provenance_inventory.scan(scope=scope, progress=progress)
    items = [_application_item(record) for record in inventory.applications]
    return items, inventory, problems


def _application_item(record: ApplicationRecord) -> RegistryItem:
    return RegistryItem(
        item_id=record.app_id,
        kind="installed_application" if record.install_reason.value in {"explicit", "portable", "local"} else "installed_package",
        name=record.display_name or record.name,
        source="package-manager",
        version=record.version,
        manager=record.manager,
        path=record.integrity.artifact_path,
        checksum=record.integrity.checksum,
        exportable=True,
        confidence=record.origin.confidence.value,
        metadata={
            "architecture": record.architecture,
            "install_method": record.install_method,
            "install_reason": record.install_reason.value,
            "remote_name": record.origin.remote_name,
            "remote_url": _redact_url(record.origin.remote_url),
            "branch": record.origin.branch,
            "ref": record.origin.ref,
            "commit": record.origin.commit,
            "vendor": record.origin.vendor,
            "source_package": record.origin.source_package,
            "warnings": list(record.warnings),
        },
    )


def _redact_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))


def _repository_items() -> tuple[list[RegistryItem], list[str]]:
    items: list[RegistryItem] = []
    problems: list[str] = []

    out, error = _run(["flatpak", "remotes", "--columns=name,url,options"])
    if error:
        problems.append(f"flatpak remotes: {error}")
    for line in out.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if not fields or not fields[0]:
            continue
        name = fields[0]
        url = _redact_url(fields[1] if len(fields) > 1 else "")
        options = fields[2] if len(fields) > 2 else ""
        items.append(
            RegistryItem(
                item_id=f"repository:flatpak:{name}",
                kind="package_repository",
                name=name,
                manager="flatpak",
                source="flatpak-remotes",
                exportable=True,
                metadata={"url": url, "options": options},
            )
        )

    for source in _apt_source_records():
        items.append(source)

    if Path("/etc/pacman.conf").is_file():
        try:
            text = Path("/etc/pacman.conf").read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"pacman.conf: {exc}")
        else:
            for section in re.findall(r"^\[([^]]+)\]", text, flags=re.MULTILINE):
                if section.lower() == "options":
                    continue
                items.append(
                    RegistryItem(
                        item_id=f"repository:pacman:{section}",
                        kind="package_repository",
                        name=section,
                        manager="pacman",
                        source="pacman.conf",
                        exportable=True,
                    )
                )

    out, error = _run(["dnf", "repo", "list", "--enabled"])
    if error:
        out, error = _run(["dnf", "repolist", "--enabled"])
    if error:
        problems.append(f"dnf repositories: {error}")
    for line in out.splitlines():
        if not line.strip() or line.lower().startswith(("repo id", "id de repo", "last metadata")):
            continue
        repo_id = line.split()[0]
        if repo_id:
            items.append(
                RegistryItem(
                    item_id=f"repository:dnf:{repo_id}",
                    kind="package_repository",
                    name=repo_id,
                    manager="dnf",
                    source="dnf-repolist",
                    exportable=True,
                )
            )

    out, error = _run(["zypper", "--no-refresh", "repos", "--uri"])
    if error:
        problems.append(f"zypper repositories: {error}")
    for line in out.splitlines():
        if "|" not in line:
            continue
        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 3 or not columns[1] or columns[1].lower() == "alias":
            continue
        alias = columns[1]
        uri = next((column for column in columns if column.startswith(("http:", "https:", "file:", "dir:"))), "")
        items.append(
            RegistryItem(
                item_id=f"repository:zypper:{alias}",
                kind="package_repository",
                name=alias,
                manager="zypper",
                source="zypper-repos",
                exportable=True,
                metadata={"url": _redact_url(uri)},
            )
        )
    return _dedupe_items(items), problems


def _apt_source_records() -> list[RegistryItem]:
    paths = [Path("/etc/apt/sources.list")]
    directory = Path("/etc/apt/sources.list.d")
    if directory.is_dir():
        paths.extend(sorted(directory.glob("*.list")))
        paths.extend(sorted(directory.glob("*.sources")))
    results: list[RegistryItem] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        uris: list[str] = []
        if path.suffix == ".sources":
            for raw in text.splitlines():
                if raw.lower().startswith("uris:"):
                    uris.extend(raw.split(":", 1)[1].split())
        else:
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or not line.startswith(("deb ", "deb-src ")):
                    continue
                fields = line.split()
                for field in fields[1:]:
                    if field.startswith(("http://", "https://", "file:", "cdrom:")):
                        uris.append(field)
                        break
        for uri in uris:
            redacted = _redact_url(uri)
            identity = hashlib.blake2b(redacted.encode("utf-8"), digest_size=8).hexdigest()
            item_id = f"repository:apt:{identity}"
            if item_id in seen:
                continue
            seen.add(item_id)
            results.append(
                RegistryItem(
                    item_id=item_id,
                    kind="package_repository",
                    name=redacted,
                    manager="apt",
                    source=_portable_path(path, None),
                    exportable=True,
                    metadata={"url": redacted},
                )
            )
    return results


def _service_items(system_only: bool) -> tuple[list[RegistryItem], list[str]]:
    commands = [["systemctl", "list-unit-files", "--type=service", "--state=enabled", "--no-legend", "--no-pager"]]
    if not system_only:
        commands.append(["systemctl", "--user", "list-unit-files", "--type=service", "--state=enabled", "--no-legend", "--no-pager"])
    items: list[RegistryItem] = []
    problems: list[str] = []
    for command in commands:
        out, error = _run(command, timeout=15)
        scope = "user" if "--user" in command else "system"
        if error:
            # systemd puede no existir (contenedores, WSL, otros init). No es fatal.
            problems.append(f"systemctl {scope}: {error}")
            continue
        for line in out.splitlines():
            fields = line.split()
            if not fields:
                continue
            name = fields[0]
            state = fields[1] if len(fields) > 1 else "enabled"
            items.append(
                RegistryItem(
                    item_id=f"service:{scope}:{name}",
                    kind="enabled_service",
                    name=name,
                    source="systemctl-list-unit-files",
                    exportable=True,
                    metadata={"scope": scope, "state": state},
                )
            )
    return items, problems


def _system_identity(home: Path | None) -> dict[str, Any]:
    os_release: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                os_release[key] = value.strip().strip('"')
    except OSError:
        pass
    return {
        "distro_id": os_release.get("ID", ""),
        "distro_version": os_release.get("VERSION_ID", ""),
        "pretty_name": os_release.get("PRETTY_NAME", platform.platform()),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", ""),
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "shell": Path(os.environ.get("SHELL", "")).name,
        "home_scanned": bool(home),
    }


def _dedupe_items(items: Iterable[RegistryItem]) -> list[RegistryItem]:
    best: dict[str, RegistryItem] = {}
    for item in items:
        current = best.get(item.item_id)
        if current is None or (not current.checksum and item.checksum):
            best[item.item_id] = item
    return sorted(best.values(), key=lambda item: (item.kind, item.manager, item.name.lower(), item.item_id))


def capture_snapshot(
    *,
    phase: str = "manual",
    home: str | Path | None = None,
    system_only: bool = False,
    progress: ProgressHook = None,
) -> tuple[RegistrySnapshot, Inventory | None]:
    """Toma una fotografía de solo lectura sin explorar contenido personal."""
    home_path = None if system_only else Path(home).expanduser() if home else Path.home()
    items: list[RegistryItem] = []
    problems: list[str] = []

    package_items, inventory, package_problems = _package_items("all", progress=progress)
    items.extend(package_items)
    problems.extend(package_problems)

    repo_items, repo_problems = _repository_items()
    items.extend(repo_items)
    problems.extend(repo_problems)

    service_items, service_problems = _service_items(system_only)
    items.extend(service_items)
    problems.extend(service_problems)

    items.extend(_system_configuration_items())
    items.extend(_tool_items(home_path))
    if home_path is not None:
        user_items, user_problems = _user_configuration_items(home_path)
        items.extend(user_items)
        problems.extend(user_problems)

    snapshot = RegistrySnapshot(
        snapshot_id=uuid.uuid4().hex[:12],
        captured_at=time.time(),
        phase=phase,
        system_only=system_only,
        system=_system_identity(home_path),
        managers_seen=sorted(set(inventory.managers_seen if inventory else [])),
        items=_dedupe_items(items),
        problems=problems,
    )
    return snapshot, inventory


def save_snapshot(snapshot: RegistrySnapshot, root: str | Path, *, pointer: str = CURRENT_POINTER) -> str:
    directory = registry_dir(root)
    target = directory / SNAPSHOTS_DIRNAME / f"{snapshot.snapshot_id}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(target)
    _write_pointer(directory / pointer, snapshot.snapshot_id)
    return str(target)


def _write_pointer(path: Path, snapshot_id: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"snapshot_id": snapshot_id}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_snapshot(snapshot_id: str, root: str | Path) -> RegistrySnapshot:
    path = registry_dir(root) / SNAPSHOTS_DIRNAME / f"{snapshot_id}.json"
    if not path.is_file():
        raise RegistryError(f"No existe la fotografía de registro: {snapshot_id}")
    return RegistrySnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_pointer(root: str | Path, pointer: str = CURRENT_POINTER) -> RegistrySnapshot | None:
    path = registry_dir(root) / pointer
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return load_snapshot(str(data["snapshot_id"]), root)
    except (OSError, ValueError, KeyError, RegistryError):
        return None


def compare_snapshots(before: RegistrySnapshot, after: RegistrySnapshot, *, session: str) -> list[RegistryEvent]:
    old = before.by_id()
    new = after.by_id()
    events: list[RegistryEvent] = []
    now = after.captured_at
    for item_id in sorted(set(old) | set(new)):
        previous = old.get(item_id)
        current = new.get(item_id)
        if previous is None and current is not None:
            events.append(_event(now, session, "added", current, None, current))
        elif current is None and previous is not None:
            events.append(_event(now, session, "removed", previous, previous, None))
        elif previous is not None and current is not None and _item_identity(previous) != _item_identity(current):
            events.append(_event(now, session, "changed", current, previous, current))
    return events


def _event(
    occurred_at: float,
    session: str,
    change: str,
    item: RegistryItem,
    before: RegistryItem | None,
    after: RegistryItem | None,
) -> RegistryEvent:
    return RegistryEvent(
        event_id=uuid.uuid4().hex[:12],
        occurred_at=occurred_at,
        session=session,
        change=change,
        item_id=item.item_id,
        kind=item.kind,
        name=item.name,
        exportable=item.exportable,
        before=before.to_dict() if before else None,
        after=after.to_dict() if after else None,
    )


def _item_identity(item: RegistryItem) -> tuple[Any, ...]:
    return (
        item.kind,
        item.version,
        item.manager,
        item.path,
        item.checksum,
        item.size,
        item.mode,
        item.exportable,
        json.dumps(item.metadata, sort_keys=True, ensure_ascii=False),
    )


def append_events(events: Sequence[RegistryEvent], root: str | Path) -> str:
    path = registry_dir(root) / EVENTS_FILENAME
    if not events:
        path.touch(exist_ok=True)
        return str(path)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return str(path)


def update_registry(
    *,
    root: str | Path,
    phase: str,
    home: str | Path | None = None,
    system_only: bool = False,
    session: str | None = None,
    set_baseline_if_missing: bool = True,
    progress: ProgressHook = None,
) -> RegistryUpdate:
    directory = registry_dir(root)
    previous = load_pointer(root, CURRENT_POINTER)
    snapshot, inventory = capture_snapshot(phase=phase, home=home, system_only=system_only, progress=progress)
    path = save_snapshot(snapshot, root, pointer=CURRENT_POINTER)
    if inventory is not None:
        # Mantiene sincronizado el inventario técnico usado por el Constructor.
        provenance_inventory.save_inventory(inventory, root=root)

    events = compare_snapshots(previous, snapshot, session=session or phase) if previous else []
    append_events(events, root)

    baseline_created = False
    if set_baseline_if_missing and load_pointer(root, BASELINE_POINTER) is None:
        _write_pointer(directory / BASELINE_POINTER, snapshot.snapshot_id)
        baseline_created = True
    return RegistryUpdate(snapshot, tuple(events), baseline_created, path)


def install_pre(*, root: str | Path, home: str | Path | None = None, system_only: bool = False) -> RegistryUpdate:
    update = update_registry(
        root=root,
        phase="install-pre",
        home=home,
        system_only=system_only,
        session="styler-installation-pre",
        set_baseline_if_missing=False,
    )
    _write_pointer(registry_dir(root) / INSTALL_PRE_POINTER, update.snapshot.snapshot_id)
    return update


def install_post(*, root: str | Path, home: str | Path | None = None, system_only: bool = False) -> RegistryUpdate:
    directory = registry_dir(root)
    before = load_pointer(root, INSTALL_PRE_POINTER) or load_pointer(root, CURRENT_POINTER)
    snapshot, inventory = capture_snapshot(phase="install-post", home=home, system_only=system_only)
    path = save_snapshot(snapshot, root, pointer=CURRENT_POINTER)
    _write_pointer(directory / INSTALL_POST_POINTER, snapshot.snapshot_id)
    if inventory is not None:
        provenance_inventory.save_inventory(inventory, root=root)
    events = compare_snapshots(before, snapshot, session="styler-installation") if before else []
    append_events(events, root)
    baseline_created = load_pointer(root, BASELINE_POINTER) is None
    if baseline_created:
        _write_pointer(directory / BASELINE_POINTER, snapshot.snapshot_id)
    return RegistryUpdate(snapshot, tuple(events), baseline_created, path)


def bootstrap_registry(
    *,
    root: str | Path,
    home: str | Path | None = None,
    system_only: bool = False,
    phase: str = "automatic-bootstrap",
) -> RegistryUpdate:
    return update_registry(
        root=root,
        phase=phase,
        home=home,
        system_only=system_only,
        session=phase,
        set_baseline_if_missing=True,
    )


def automatic_startup_scan(
    *,
    root: str | Path,
    home: str | Path | None = None,
    demo: bool = False,
    min_interval_seconds: float = 60.0,
) -> RegistryUpdate | None:
    """Actualiza el registro al abrir Styler, sin bloquear el modo demo.

    El intervalo evita repetir inmediatamente el escaneo que acaba de ejecutar
    el instalador. Cada apertura posterior compara contra el último estado.
    """
    if demo or os.environ.get("STYLER_DISABLE_AUTO_SCAN") == "1":
        return None
    current = load_pointer(root, CURRENT_POINTER)
    if current and time.time() - current.captured_at < min_interval_seconds:
        return None
    try:
        return bootstrap_registry(root=root, home=home, system_only=False, phase="application-startup")
    except Exception as exc:  # noqa: BLE001 — el registro no debe impedir abrir Styler
        path = registry_dir(root) / LAST_ERROR_FILENAME
        path.write_text(
            json.dumps({"at": time.time(), "error": str(exc)}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return None


def registry_status(root: str | Path) -> dict[str, Any]:
    current = load_pointer(root, CURRENT_POINTER)
    baseline = load_pointer(root, BASELINE_POINTER)
    event_path = registry_dir(root) / EVENTS_FILENAME
    event_count = 0
    if event_path.is_file():
        try:
            with event_path.open(encoding="utf-8") as handle:
                event_count = sum(1 for line in handle if line.strip())
        except OSError:
            pass
    return {
        "initialized": current is not None,
        "current_snapshot": current.snapshot_id if current else "",
        "baseline_snapshot": baseline.snapshot_id if baseline else "",
        "captured_at": current.captured_at if current else 0,
        "phase": current.phase if current else "",
        "system_only": current.system_only if current else False,
        "managers_seen": current.managers_seen if current else [],
        "counts": current.counts() if current else {},
        "events": event_count,
        "problems": current.problems if current else [],
    }


def _print_update(update: RegistryUpdate, *, as_json: bool) -> None:
    payload = {
        "snapshot": update.snapshot.to_dict(),
        "events": [event.to_dict() for event in update.events],
        "baseline_created": update.baseline_created,
        "snapshot_path": update.snapshot_path,
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"Registro guardado: {update.snapshot.snapshot_id}")
    print(f"Fase: {update.snapshot.phase}")
    print(f"Elementos: {len(update.snapshot.items)}")
    print(f"Cambios nuevos: {len(update.events)}")
    print(f"Gestores: {', '.join(update.snapshot.managers_seen) or 'ninguno'}")
    if update.baseline_created:
        print("Línea base creada automáticamente.")
    if update.snapshot.problems:
        print(f"Avisos no fatales: {len(update.snapshot.problems)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m styler.system_registry")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install-pre", "install-post", "bootstrap", "scan"):
        command = sub.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--home", default=None)
        command.add_argument("--system-only", action="store_true")
        command.add_argument("--phase", default="")
        command.add_argument("--json", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--root", required=True)
    status.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        payload = registry_status(args.root)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print("Registro automático de Styler")
            print(f"  Inicializado: {'sí' if payload['initialized'] else 'no'}")
            print(f"  Fotografía actual: {payload['current_snapshot'] or '—'}")
            print(f"  Línea base: {payload['baseline_snapshot'] or '—'}")
            print(f"  Eventos: {payload['events']}")
            print(f"  Gestores: {', '.join(payload['managers_seen']) or 'ninguno'}")
        return 0

    kwargs = {"root": args.root, "home": args.home, "system_only": args.system_only}
    if args.command == "install-pre":
        update = install_pre(**kwargs)
    elif args.command == "install-post":
        update = install_post(**kwargs)
    elif args.command == "bootstrap":
        update = bootstrap_registry(**kwargs, phase=args.phase or "automatic-bootstrap")
    else:
        update = update_registry(**kwargs, phase=args.phase or "manual-scan", session=args.phase or "manual-scan")
    _print_update(update, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
