"""
styler.observers.base
========================
Un Observer produce evidencia técnica (paquetes, archivos, servicios) para
una State. Cada observer es independiente: puede fallar o no aplicar en
una máquina sin tumbar la captura completa.
"""

from __future__ import annotations

from typing import Protocol

from styler.models import Package, FileEntry, ServiceEntry


class Observer(Protocol):
    name: str

    def packages(self) -> list[Package]:
        ...

    def files(self) -> list[FileEntry]:
        ...

    def services(self) -> list[ServiceEntry]:
        ...


class BaseObserver:
    """Implementación por defecto: cada método regresa vacío salvo que
    la subclase lo sobreescriba. Así un observer de solo-archivos no
    necesita implementar packages()/services()."""

    name = "base"

    def packages(self) -> list[Package]:
        return []

    def files(self) -> list[FileEntry]:
        return []

    def services(self) -> list[ServiceEntry]:
        return []

    def safe_run(self, fn):
        """Ejecuta fn() y traga excepciones para no tumbar la captura
        completa si este observer no aplica en la máquina actual."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — captura intencional y amplia
            print(f"[styler] observer '{self.name}' fallo parcial: {exc}")
            return []
