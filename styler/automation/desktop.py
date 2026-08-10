"""Driver semántico de escritorio inspirado en Selenium.

El backend preferido usa AT-SPI cuando ``pyatspi`` está disponible. Los
localizadores buscan por aplicación, rol, nombre y description; no dependen de
coordenadas de pantalla. Un backend ausente produce un diagnóstico explícito y
la espera se marca como imposible en vez de gastar todo el timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .conditions import ConditionState


@dataclass(frozen=True)
class ElementLocator:
    application: str = ""
    role: str = ""
    name: str = ""
    description: str = ""

    @classmethod
    def from_mapping(cls, data: Any) -> "ElementLocator":
        if not isinstance(data, dict):
            raise ValueError("locator debe ser un objeto.")
        locator = cls(
            application=str(data.get("application", data.get("application_id", ""))),
            role=str(data.get("role", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
        )
        if not any((locator.application, locator.role, locator.name, locator.description)):
            raise ValueError("locator necesita al menos application, role, name o description.")
        return locator

    def to_dict(self) -> dict[str, str]:
        return {
            "application": self.application,
            "role": self.role,
            "name": self.name,
            "description": self.description,
        }


@dataclass(frozen=True)
class ElementSnapshot:
    application: str
    role: str
    name: str
    description: str
    visible: bool
    enabled: bool
    focusable: bool
    actions: tuple[str, ...] = ()
    backend_ref: Any = None

    @property
    def clickable(self) -> bool:
        return self.visible and self.enabled and any(
            action.lower() in {"click", "press", "activate", "action"}
            for action in self.actions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "visible": self.visible,
            "enabled": self.enabled,
            "focusable": self.focusable,
            "actions": list(self.actions),
        }


class DesktopDriver(Protocol):
    name: str

    def available(self) -> bool: ...
    def unavailable_reason(self) -> str: ...
    def find_all(self, locator: ElementLocator) -> tuple[ElementSnapshot, ...]: ...
    def click(self, locator: ElementLocator) -> ElementSnapshot: ...


class PyAtSpiDriver:
    name = "at-spi"

    def __init__(self, module: Any | None = None) -> None:
        self._module = module
        self._error = ""
        if module is None:
            try:
                import pyatspi  # type: ignore

                self._module = pyatspi
            except Exception as exc:  # optional system dependency
                self._error = f"pyatspi no está disponible: {type(exc).__name__}: {exc}"

    def available(self) -> bool:
        return self._module is not None

    def unavailable_reason(self) -> str:
        return self._error or "AT-SPI no está disponible en esta sesión."

    def find_all(self, locator: ElementLocator) -> tuple[ElementSnapshot, ...]:
        if self._module is None:
            return ()
        desktop = self._module.Registry.getDesktop(0)
        found: list[ElementSnapshot] = []
        for application in self._children(desktop):
            application_name = self._safe(lambda: application.name, "")
            if locator.application and locator.application.lower() not in application_name.lower():
                continue
            self._walk(application, application_name, locator, found)
        return tuple(found)

    def click(self, locator: ElementLocator) -> ElementSnapshot:
        matches = self.find_all(locator)
        if not matches:
            raise LookupError(f"No se encontró el control {locator.to_dict()}.")
        snapshot = next((item for item in matches if item.clickable), matches[0])
        node = snapshot.backend_ref
        try:
            iface = node.queryAction()
        except Exception as exc:
            raise RuntimeError(f"El control no expone acciones AT-SPI: {exc}") from exc
        for index in range(iface.nActions):
            name = str(iface.getName(index) or "").lower()
            if name in {"click", "press", "activate", "action"}:
                if not iface.doAction(index):
                    raise RuntimeError(f"AT-SPI rechazó la acción '{name}'.")
                return snapshot
        raise RuntimeError("El control no tiene una acción click/press/activate.")

    def _walk(
        self,
        node: Any,
        application_name: str,
        locator: ElementLocator,
        found: list[ElementSnapshot],
    ) -> None:
        snapshot = self._snapshot(node, application_name)
        if self._matches(snapshot, locator):
            found.append(snapshot)
        for child in self._children(node):
            self._walk(child, application_name, locator, found)

    @staticmethod
    def _matches(snapshot: ElementSnapshot, locator: ElementLocator) -> bool:
        return all(
            (
                not locator.application
                or locator.application.lower() in snapshot.application.lower(),
                not locator.role or locator.role.lower() in snapshot.role.lower(),
                not locator.name or locator.name.lower() in snapshot.name.lower(),
                not locator.description
                or locator.description.lower() in snapshot.description.lower(),
            )
        )

    def _snapshot(self, node: Any, application_name: str) -> ElementSnapshot:
        module = self._module
        name = self._safe(lambda: node.name, "")
        description = self._safe(lambda: node.description, "")
        role = self._safe(lambda: node.getRoleName(), "unknown")
        states: set[Any] = set()
        try:
            state_set = node.getState()
            for state in (
                module.STATE_VISIBLE,
                module.STATE_SHOWING,
                module.STATE_ENABLED,
                module.STATE_SENSITIVE,
                module.STATE_FOCUSABLE,
            ):
                if state_set.contains(state):
                    states.add(state)
        except Exception:
            pass
        actions: list[str] = []
        try:
            iface = node.queryAction()
            actions = [str(iface.getName(index) or "") for index in range(iface.nActions)]
        except Exception:
            pass
        visible = module.STATE_VISIBLE in states or module.STATE_SHOWING in states
        enabled = module.STATE_ENABLED in states or module.STATE_SENSITIVE in states
        focusable = module.STATE_FOCUSABLE in states
        return ElementSnapshot(
            application=application_name,
            role=role,
            name=name,
            description=description,
            visible=visible,
            enabled=enabled,
            focusable=focusable,
            actions=tuple(actions),
            backend_ref=node,
        )

    @staticmethod
    def _children(node: Any) -> Iterable[Any]:
        try:
            return tuple(node[index] for index in range(node.childCount))
        except Exception:
            return ()

    @staticmethod
    def _safe(callback, default):
        try:
            return callback()
        except Exception:
            return default


class AutoDesktopDriver:
    name = "auto"

    def __init__(self, drivers: tuple[DesktopDriver, ...] | None = None) -> None:
        self.drivers = drivers or (PyAtSpiDriver(),)

    def _active(self) -> DesktopDriver | None:
        return next((driver for driver in self.drivers if driver.available()), None)

    def available(self) -> bool:
        return self._active() is not None

    def unavailable_reason(self) -> str:
        return "; ".join(driver.unavailable_reason() for driver in self.drivers)

    def find_all(self, locator: ElementLocator) -> tuple[ElementSnapshot, ...]:
        driver = self._active()
        return driver.find_all(locator) if driver else ()

    def click(self, locator: ElementLocator) -> ElementSnapshot:
        driver = self._active()
        if driver is None:
            raise RuntimeError(self.unavailable_reason())
        return driver.click(locator)


class DesktopElementCondition:
    def __init__(
        self,
        driver: DesktopDriver,
        locator: ElementLocator,
        *,
        expectation: str = "present",
    ) -> None:
        if expectation not in {"present", "visible", "enabled", "clickable"}:
            raise ValueError(f"Expectativa de elemento desconocida: {expectation}.")
        self.driver = driver
        self.locator = locator
        self.expectation = expectation
        self.name = f"elemento {expectation}: {locator.to_dict()}"
        self._matches: tuple[ElementSnapshot, ...] = ()
        self._detail = "sin evaluar"

    def state(self) -> ConditionState:
        if not self.driver.available():
            self._detail = self.driver.unavailable_reason()
            return ConditionState.UNSATISFIABLE
        self._matches = self.driver.find_all(self.locator)
        if self.expectation == "present":
            satisfied = bool(self._matches)
        elif self.expectation == "visible":
            satisfied = any(item.visible for item in self._matches)
        elif self.expectation == "enabled":
            satisfied = any(item.visible and item.enabled for item in self._matches)
        else:
            satisfied = any(item.clickable for item in self._matches)
        self._detail = (
            f"backend={self.driver.name} matches={len(self._matches)} "
            f"snapshots={[item.to_dict() for item in self._matches[:5]]}"
        )
        return ConditionState.SATISFIED if satisfied else ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return self._detail
