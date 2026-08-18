"""
styler.resolvers
================
Un *resolutor* sabe hablar con **un** gestor de paquetes: si tiene una
aplicación, cómo instalarla, cómo actualizarla y cómo comprobarla.

Esto es el único hardcoding legítimo del proyecto: el protocolo de cada gestor
(`apt-get install`, `pacman -S`) es una capacidad técnica estable. Lo que NO vive
aquí es el conocimiento del mundo («KDE en Ubuntu se llama así»): eso está en los
catálogos TOML.

Gracias a `discover()`, Styler puede satisfacer una aplicación en una
distribución que nunca vio: le pregunta al gestor si la tiene, en vez de
consultar una tabla escrita a mano.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from styler.execution.processes import CommandResult, Runner


@dataclass(frozen=True)
class Candidate:
    """Una forma concreta de satisfacer un requisito en ESTE equipo."""

    manager: str
    package: str
    reason: str = ""              # cómo se descubrió
    privileged: bool = True

    @property
    def key(self) -> str:
        return f"{self.manager}:{self.package}"


class Resolver(Protocol):
    manager: str

    def available(self, runner: Runner) -> bool: ...
    def offers(self, package: str, runner: Runner) -> bool | None: ...
    def installed(self, package: str, runner: Runner) -> bool | None: ...
    def install_argv(self, package: str, prefix: list[str]) -> list[str]: ...
    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]: ...
    def refresh_argv(self, prefix: list[str]) -> list[str] | None: ...
    def up_to_date(self, result: CommandResult) -> bool: ...


@dataclass
class _Base:
    manager: str = ""
    program: str = ""
    privileged: bool = True

    def available(self, runner: Runner) -> bool:
        return runner.available(self.program)

    def _rc(self, argv: list[str], runner: Runner) -> CommandResult:
        return runner.run(argv, timeout=60)

    def refresh_argv(self, prefix: list[str]) -> list[str] | None:
        return None

    def up_to_date(self, result: CommandResult) -> bool:
        text = (result.stdout or "").lower()
        return any(
            marker in text
            for marker in (
                "0 upgraded, 0 newly installed",
                "nothing to do",
                "there is nothing to do",
                "no packages marked for update",
                "nothing provides",
                "is up to date",
            )
        )


@dataclass
class AptResolver(_Base):
    manager: str = "apt"
    program: str = "apt-get"

    def offers(self, package: str, runner: Runner) -> bool | None:
        if not runner.available("apt-cache"):
            return None
        result = self._rc(["apt-cache", "policy", package], runner)
        if result.returncode != 0:
            return False
        text = result.stdout.strip()
        if not text:
            return None
        if "candidate: (none)" in text.lower():
            return False
        return True

    def installed(self, package: str, runner: Runner) -> bool | None:
        if not runner.available("dpkg-query"):
            return None
        result = self._rc(
            ["dpkg-query", "-W", "-f=${db:Status-Status}", package], runner
        )
        return result.returncode == 0 and "installed" in result.stdout

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        # Política no interactiva + espera del bloqueo de dpkg: «-y» solo responde
        # a APT, no a las preguntas de debconf (p. ej. elegir el display manager).
        from styler.package_commands import apt_install_argv

        return apt_install_argv(prefix, package)

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        from styler.package_commands import apt_install_argv

        argv = apt_install_argv(prefix, package)
        return [*argv[:-1], "--only-upgrade", argv[-1]]

    def refresh_argv(self, prefix: list[str]) -> list[str] | None:
        from styler.package_commands import apt_update_argv

        return apt_update_argv(prefix)


@dataclass
class PacmanResolver(_Base):
    manager: str = "pacman"
    program: str = "pacman"

    def offers(self, package: str, runner: Runner) -> bool | None:
        return self._rc(["pacman", "-Si", package], runner).returncode == 0

    def installed(self, package: str, runner: Runner) -> bool | None:
        return self._rc(["pacman", "-Q", package], runner).returncode == 0

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "pacman", "-S", "--needed", "--noconfirm", package]

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "pacman", "-S", "--noconfirm", package]

    def refresh_argv(self, prefix: list[str]) -> list[str] | None:
        return [*prefix, "pacman", "-Sy", "--noconfirm"]


@dataclass
class DnfResolver(_Base):
    manager: str = "dnf"
    program: str = "dnf"

    def offers(self, package: str, runner: Runner) -> bool | None:
        if package.startswith("@"):   # grupo: se asume existente si dnf existe
            return True
        return self._rc(["dnf", "info", package], runner).returncode == 0

    def installed(self, package: str, runner: Runner) -> bool | None:
        if package.startswith("@"):
            return None
        if not runner.available("rpm"):
            return None
        return self._rc(["rpm", "-q", package], runner).returncode == 0

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "dnf", "install", "-y", package]

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "dnf", "upgrade", "-y", package]

    def refresh_argv(self, prefix: list[str]) -> list[str] | None:
        return [*prefix, "dnf", "makecache", "--refresh"]


@dataclass
class ZypperResolver(_Base):
    manager: str = "zypper"
    program: str = "zypper"

    def offers(self, package: str, runner: Runner) -> bool | None:
        return self._rc(
            ["zypper", "--non-interactive", "search", "--match-exact", package], runner
        ).returncode == 0

    def installed(self, package: str, runner: Runner) -> bool | None:
        if not runner.available("rpm"):
            return None
        return self._rc(["rpm", "-q", package], runner).returncode == 0

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "zypper", "--non-interactive", "install", package]

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "zypper", "--non-interactive", "update", package]

    def refresh_argv(self, prefix: list[str]) -> list[str] | None:
        return [*prefix, "zypper", "--non-interactive", "refresh"]


@dataclass
class FlatpakResolver(_Base):
    manager: str = "flatpak"
    program: str = "flatpak"
    privileged: bool = False   # Flatpak de usuario no necesita administrador

    def offers(self, package: str, runner: Runner) -> bool | None:
        result = self._rc(["flatpak", "remote-info", "flathub", package], runner)
        return result.returncode == 0

    def installed(self, package: str, runner: Runner) -> bool | None:
        return self._rc(["flatpak", "info", package], runner).returncode == 0

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        return ["flatpak", "install", "-y", "flathub", package]

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        return ["flatpak", "update", "-y", package]


@dataclass
class SnapResolver(_Base):
    manager: str = "snap"
    program: str = "snap"

    def offers(self, package: str, runner: Runner) -> bool | None:
        return self._rc(["snap", "info", package], runner).returncode == 0

    def installed(self, package: str, runner: Runner) -> bool | None:
        return self._rc(["snap", "list", package], runner).returncode == 0

    def install_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "snap", "install", package]

    def upgrade_argv(self, package: str, prefix: list[str]) -> list[str]:
        return [*prefix, "snap", "refresh", package]


REGISTRY: dict[str, Resolver] = {
    resolver.manager: resolver
    for resolver in (
        AptResolver(),
        PacmanResolver(),
        DnfResolver(),
        ZypperResolver(),
        FlatpakResolver(),
        SnapResolver(),
    )
}


def resolver_for(manager: str) -> Optional[Resolver]:
    return REGISTRY.get(manager)


def supported_managers() -> tuple[str, ...]:
    return tuple(REGISTRY)
