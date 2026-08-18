"""Simulador de procesos/gestores exclusivo de la suite de pruebas."""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.execution.processes import CommandResult

@dataclass
class FakeRunner:
    """Un equipo simulado: qué programas hay, qué está instalado, qué se ofrece.

    - `programs`: ejecutables presentes en el PATH.
    - `installed`: identificadores «gestor:paquete» ya instalados.
    - `offered`: lo que cada gestor puede entregar. `None` = lo tiene todo.
    - `failing`: nombres cuya instalación falla.
    - `remotes`: remotos de Flatpak configurados.
    - `provides`: al instalar «gestor:paquete», aparecen estos ejecutables.
    """

    programs: set[str] = field(default_factory=set)
    installed: set[str] = field(default_factory=set)
    offered: set[str] | None = None
    failing: set[str] = field(default_factory=set)
    remotes: set[str] = field(default_factory=set)
    provides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    upgradable: set[str] = field(default_factory=set)
    lying: set[str] = field(default_factory=set)   # sale 0 pero no deja nada instalado
    calls: list[list[str]] = field(default_factory=list)

    # -- API de Runner ---------------------------------------------------
    def available(self, program: str) -> bool:
        return program in self.programs

    def run(self, argv: list[str], timeout: float | None = None) -> CommandResult:
        self.calls.append(list(argv))
        if not argv:
            # Un comando vacío es un fallo del planificador, no un éxito.
            return CommandResult(127, "", "Comando vacío")
        words = [item for item in argv if not item.startswith("-")]
        program = next((word for word in words if word in _PROGRAMS), "")
        package = words[-1] if len(words) > 1 else ""

        handler = _HANDLERS.get(program)
        if handler is None:
            return CommandResult(0)
        return handler(self, argv, words, package)

    # -- ayudas ----------------------------------------------------------
    def _offers(self, manager: str, package: str) -> bool:
        if self.offered is None:
            return True
        return f"{manager}:{package}" in self.offered

    def _install(self, manager: str, package: str) -> CommandResult:
        if package in self.failing or f"{manager}:{package}" in self.failing:
            return CommandResult(100, "", f"E: no se pudo instalar {package}")
        if not self._offers(manager, package):
            return CommandResult(100, "", f"E: Unable to locate package {package}")
        if package in self.lying or f"{manager}:{package}" in self.lying:
            return CommandResult(0, f"Setting up {package} ...")   # miente: no instala
        self.installed.add(f"{manager}:{package}")
        for program in self.provides.get(f"{manager}:{package}", ()):
            self.programs.add(program)
        return CommandResult(0, f"Setting up {package} ...")

    def _upgrade(self, manager: str, package: str) -> CommandResult:
        key = f"{manager}:{package}"
        if package in self.failing or key in self.failing:
            return CommandResult(100, "", f"E: no se pudo actualizar {package}")
        if key in self.upgradable:
            self.upgradable.discard(key)
            return CommandResult(0, f"1 upgraded, 0 newly installed. {package}")
        return CommandResult(0, "0 upgraded, 0 newly installed, 0 to remove.")


def _apt(runner: FakeRunner, argv: list[str], words: list[str], package: str) -> CommandResult:
    if "update" in words:
        return CommandResult(0, "Reading package lists...")
    if "install" in words:
        if "--only-upgrade" in argv:
            return runner._upgrade("apt", package)
        return runner._install("apt", package)
    return CommandResult(0)


def _apt_cache(runner: FakeRunner, argv, words, package) -> CommandResult:
    if runner._offers("apt", package):
        return CommandResult(0, f"{package}:\n  Installed: (none)\n  Candidate: 1.0\n")
    return CommandResult(0, f"{package}:\n  Installed: (none)\n  Candidate: (none)\n")


def _dpkg_query(runner: FakeRunner, argv, words, package) -> CommandResult:
    if f"apt:{package}" in runner.installed:
        return CommandResult(0, "installed")
    return CommandResult(1, "", "no packages found")


def _pacman(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "-Q" in argv:
        return CommandResult(0) if f"pacman:{package}" in runner.installed else CommandResult(1)
    if "-Si" in argv:
        return CommandResult(0) if runner._offers("pacman", package) else CommandResult(1)
    if "-Sy" in argv:
        return CommandResult(0)
    if "-S" in argv:
        if f"pacman:{package}" in runner.installed:
            return runner._upgrade("pacman", package)
        return runner._install("pacman", package)
    return CommandResult(0)


def _dnf(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "makecache" in words:
        return CommandResult(0, "Metadata cache created.")
    if "info" in words:
        return CommandResult(0) if runner._offers("dnf", package) else CommandResult(1)
    if "install" in words:
        return runner._install("dnf", package)
    if "upgrade" in words:
        return runner._upgrade("dnf", package)
    return CommandResult(0)


def _rpm(runner: FakeRunner, argv, words, package) -> CommandResult:
    present = f"dnf:{package}" in runner.installed or f"zypper:{package}" in runner.installed
    return CommandResult(0) if present else CommandResult(1)


def _zypper(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "refresh" in words:
        return CommandResult(0, "Repository metadata refreshed.")
    if "search" in words:
        return CommandResult(0) if runner._offers("zypper", package) else CommandResult(104)
    if "install" in words:
        return runner._install("zypper", package)
    if "update" in words:
        return runner._upgrade("zypper", package)
    return CommandResult(0)


def _flatpak(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "remotes" in words:
        return CommandResult(0, "\n".join(sorted(runner.remotes)))
    if "remote-add" in words:
        runner.remotes.add(words[-2] if len(words) > 2 else package)
        return CommandResult(0)
    if "remote-info" in words:
        return CommandResult(0) if runner._offers("flatpak", package) else CommandResult(1)
    if "info" in words:
        return (
            CommandResult(0)
            if f"flatpak:{package}" in runner.installed
            else CommandResult(1)
        )
    if "install" in words:
        return runner._install("flatpak", package)
    if "update" in words:
        return runner._upgrade("flatpak", package)
    return CommandResult(0)


def _snap(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "list" in words:
        return CommandResult(0) if f"snap:{package}" in runner.installed else CommandResult(1)
    if "info" in words:
        return CommandResult(0) if runner._offers("snap", package) else CommandResult(1)
    if "install" in words:
        return runner._install("snap", package)
    if "refresh" in words:
        return runner._upgrade("snap", package)
    return CommandResult(0)


_HANDLERS = {
    "apt-get": _apt,
    "apt-cache": _apt_cache,
    "dpkg-query": _dpkg_query,
    "pacman": _pacman,
    "dnf": _dnf,
    "rpm": _rpm,
    "zypper": _zypper,
    "flatpak": _flatpak,
    "snap": _snap,
}

_PROGRAMS = set(_HANDLERS)
