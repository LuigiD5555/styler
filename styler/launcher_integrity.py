"""Portabilidad y validación de lanzadores personalizados restaurados.

Los perfiles pueden contener scripts y archivos .desktop creados en otro HOME.
Este módulo conserva los permisos declarados, adapta referencias absolutas al
HOME actual y describe dependencias faltantes sin ejecutar la aplicación.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
import shutil

_HOME_RE = re.compile(r"/home/[A-Za-z0-9._-]+(?=/|\b)")
_SHEBANG_RE = re.compile(r"^#!")
_DESKTOP_KEYS = {"Exec", "TryExec", "Icon", "Path"}


@dataclass
class LauncherInspection:
    path: str
    changed: bool = False
    executable: bool = False
    missing_commands: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_commands and not self.missing_paths


def _is_text_candidate(path: Path, data: bytes) -> bool:
    return path.suffix in {".desktop", ".sh"} or data.startswith(b"#!")


def _rewrite_home_references(text: str, home: Path) -> str:
    return _HOME_RE.sub(str(home), text)


def _desktop_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in _DESKTOP_KEYS and key not in values:
            values[key] = value.strip()
    return values


def _expand_path(value: str, home: Path) -> Path | None:
    value = value.strip().strip('"\'')
    if not value:
        return None
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value.startswith("~/"):
        value = str(home / value[2:])
    candidate = Path(value)
    return candidate if candidate.is_absolute() else None


def _command_from_exec(value: str) -> str:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return ""
    while tokens and ("=" in tokens[0] and not tokens[0].startswith("/")):
        tokens.pop(0)
    if not tokens:
        return ""
    if tokens[0] in {"env", "sh", "bash"} and len(tokens) > 1:
        # sh -lc '...' cannot be interpreted safely here.
        if tokens[0] in {"sh", "bash"} and tokens[1] in {"-c", "-lc"}:
            return ""
        if tokens[0] == "env":
            tokens = [t for t in tokens[1:] if "=" not in t]
    return tokens[0] if tokens else ""


def _inspect_shell(text: str, home: Path, result: LauncherInspection) -> None:
    # Detecta comandos evidentes de scripts simples, como el wrapper de Affinity.
    for command in ("wine", "wine64", "flatpak", "python", "python3"):
        if re.search(rf"(?m)^\s*{re.escape(command)}(?:\s|$)", text):
            if shutil.which(command) is None:
                result.missing_commands.append(command)
    for quoted in re.findall(r'["\']([^"\']*(?:\$HOME|\$\{HOME\}|~/|/home/|/tmp/)[^"\']*)["\']', text):
        expanded = quoted.replace("${HOME}", str(home)).replace("$HOME", str(home))
        if expanded.startswith("~/"):
            expanded = str(home / expanded[2:])
        path = Path(expanded)
        # Ignora el WINEPREFIX: puede crearse al primer arranque; sí lo informa.
        if not path.exists():
            result.missing_paths.append(str(path))


def normalize_and_inspect(path: str | Path, home: str | Path, expected_mode: int | None = None) -> LauncherInspection:
    """Adapta un lanzador al HOME destino y devuelve un diagnóstico estructurado."""
    target = Path(path)
    home_path = Path(home).expanduser().resolve()
    result = LauncherInspection(path=str(target))

    if expected_mode is not None:
        os.chmod(target, expected_mode)
    result.executable = os.access(target, os.X_OK)

    try:
        data = target.read_bytes()
    except OSError as exc:
        result.notes.append(f"No se pudo inspeccionar: {exc}")
        return result
    if not _is_text_candidate(target, data):
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.notes.append("Archivo no UTF-8; se conservaron sus bytes y permisos.")
        return result

    portable = _rewrite_home_references(text, home_path)
    if portable != text:
        target.write_text(portable, encoding="utf-8")
        if expected_mode is not None:
            os.chmod(target, expected_mode)
        result.changed = True
        result.notes.append("Se adaptaron rutas absolutas del HOME de origen.")
    text = portable

    if target.suffix == ".desktop":
        values = _desktop_values(text)
        for key in ("Exec", "TryExec"):
            value = values.get(key, "")
            command = _command_from_exec(value)
            if not command:
                continue
            absolute = _expand_path(command, home_path)
            if absolute is not None:
                if not absolute.exists():
                    result.missing_paths.append(str(absolute))
                elif not os.access(absolute, os.X_OK):
                    result.notes.append(f"El destino de {key} existe pero no es ejecutable: {absolute}")
            elif shutil.which(command) is None:
                result.missing_commands.append(command)
        for key in ("Path", "Icon"):
            absolute = _expand_path(values.get(key, ""), home_path)
            if absolute is not None and not absolute.exists():
                result.missing_paths.append(str(absolute))
    elif target.suffix == ".sh" or _SHEBANG_RE.match(text):
        _inspect_shell(text, home_path, result)

    result.missing_commands = sorted(set(result.missing_commands))
    result.missing_paths = sorted(set(result.missing_paths))
    return result
