from __future__ import annotations

import shutil

from styler.models import Package
from styler.observers.base import BaseObserver
from styler.execution.processes import ProcessRunner


class FlatpakObserver(BaseObserver):
    name = "flatpak"

    def packages(self) -> list[Package]:
        return self.safe_run(self._read_flatpak)

    def _read_flatpak(self) -> list[Package]:
        if not shutil.which("flatpak"):
            return []
        result = ProcessRunner(timeout=30).run(
            ["flatpak", "list", "--columns=application,version,arch"], timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "flatpak list falló")
        out = result.stdout
        packages: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            name = parts[0]
            version = parts[1] if len(parts) > 1 else ""
            arch = parts[2] if len(parts) > 2 else ""
            packages.append(Package(manager="flatpak", name=name, version=version, architecture=arch))
        return packages
