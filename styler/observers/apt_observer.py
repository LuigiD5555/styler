"""
styler.observers.apt_observer
================================
Lee el estado de dpkg. No asume Ubuntu/Mint especificamente instalado
en el host donde corre Styler: si dpkg no existe, regresa vacío.
"""

from __future__ import annotations

import shutil

from styler.models import Package
from styler.observers.base import BaseObserver
from styler.execution.processes import ProcessRunner


class AptObserver(BaseObserver):
    name = "apt"

    def packages(self) -> list[Package]:
        return self.safe_run(self._read_dpkg)

    def _read_dpkg(self) -> list[Package]:
        if not shutil.which("dpkg-query"):
            return []
        result = ProcessRunner(timeout=30).run(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${Status}\n"],
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "dpkg-query falló")
        out = result.stdout
        packages: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, version, arch, status = parts[0], parts[1], parts[2], parts[3]
            if "installed" not in status:
                continue
            packages.append(Package(manager="apt", name=name, version=version, architecture=arch))
        return packages
