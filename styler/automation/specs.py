"""Acciones y condiciones como datos, no como código.

Hasta 0.2 las acciones eran objetos Python construidos a mano. Eso impide
grabarlas, exportarlas en un paquete, editarlas desde la interfaz o volver a
componerlas. Aquí se define su forma declarativa —``ActionSpec`` y
``ConditionSpec``— y los registros que las convierten en objetos ejecutables.

Regla de seguridad: **una receta portable es un dato, nunca un script.** El
motor conserva primitivas de proceso para flujos locales y confiables, pero el
formato importable no puede proporcionar ``argv`` arbitrario: solo referencia
aplicaciones y comprobaciones registradas por Styler. Si un paquete declara un
``kind`` desconocido o una primitiva reservada, se rechaza entero. Es la misma
frontera de confianza que aplica el catálogo de componentes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .actions import (
    Action,
    ConditionalAction,
    ForEachAction,
    LaunchProcessAction,
    NoteAction,
    RetryAction,
    SequenceAction,
    SleepAction,
    StopProcessAction,
    TimeoutAction,
    TryFinallyAction,
    WaitAction,
    DesktopClickAction,
)
from .conditions import (
    AllCondition,
    AnyCondition,
    Condition,
    CommandOutputCondition,
    CpuBelowCondition,
    DBusNameOwnedCondition,
    DirectoryQuiescentCondition,
    FileExistsCondition,
    FileStableCondition,
    GoneCondition,
    PathGlobCondition,
    ProcessAliveCondition,
    ProcessRunningCondition,
    WindowPresentCondition,
)
from .profiles import ApplicationProfile
from .desktop import AutoDesktopDriver, DesktopElementCondition, ElementLocator

from .controller import WithApplicationAction
SPEC_SCHEMA = "styler.automation/1"


class SpecError(ValueError):
    """El árbol declarativo no es válido."""


class UnknownKindError(SpecError):
    """El ``kind`` no está registrado: se rechaza en vez de interpretarse."""


@dataclass(frozen=True)
class ConditionSpec:
    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    children: tuple["ConditionSpec", ...] = ()
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.name:
            data["name"] = self.name
        if self.params:
            data["params"] = dict(self.params)
        if self.children:
            data["children"] = [child.to_dict() for child in self.children]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConditionSpec":
        if not isinstance(data, Mapping) or not data.get("kind"):
            raise SpecError("Cada condición necesita un campo 'kind'.")
        children = data.get("children") or []
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            raise SpecError("'children' debe ser una lista.")
        params = data.get("params") or {}
        if not isinstance(params, Mapping):
            raise SpecError("'params' debe ser un objeto.")
        return cls(
            kind=str(data["kind"]),
            params=dict(params),
            children=tuple(cls.from_dict(child) for child in children),
            name=str(data.get("name") or ""),
        )


@dataclass(frozen=True)
class ActionSpec:
    kind: str
    id: str = ""
    title: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    children: tuple["ActionSpec", ...] = ()
    condition: ConditionSpec | None = None
    abort_conditions: tuple[ConditionSpec, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.id:
            data["id"] = self.id
        if self.title:
            data["title"] = self.title
        if self.params:
            data["params"] = dict(self.params)
        if self.condition is not None:
            data["condition"] = self.condition.to_dict()
        if self.abort_conditions:
            data["abort_conditions"] = [item.to_dict() for item in self.abort_conditions]
        if self.children:
            data["children"] = [child.to_dict() for child in self.children]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionSpec":
        if not isinstance(data, Mapping) or not data.get("kind"):
            raise SpecError("Cada acción necesita un campo 'kind'.")
        children = data.get("children") or []
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            raise SpecError("'children' debe ser una lista.")
        params = data.get("params") or {}
        if not isinstance(params, Mapping):
            raise SpecError("'params' debe ser un objeto.")
        raw_condition = data.get("condition")
        raw_abort = data.get("abort_conditions") or []
        if not isinstance(raw_abort, Sequence) or isinstance(raw_abort, (str, bytes)):
            raise SpecError("'abort_conditions' debe ser una lista.")
        return cls(
            kind=str(data["kind"]),
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            params=dict(params),
            children=tuple(cls.from_dict(child) for child in children),
            condition=ConditionSpec.from_dict(raw_condition) if raw_condition else None,
            abort_conditions=tuple(ConditionSpec.from_dict(item) for item in raw_abort),
        )

    def walk(self) -> list["ActionSpec"]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child.walk())
        return nodes


@dataclass
class BuildContext:
    """Datos que el árbol declarativo necesita para volverse ejecutable."""

    home: Path | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    profiles: dict[str, ApplicationProfile] = field(default_factory=dict)
    applications: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "flatpak:org.gimp.GIMP": ("flatpak", "run", "org.gimp.GIMP"),
    })
    commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    desktop_driver: Any = field(default_factory=AutoDesktopDriver)

    def resolve_application(self, application_id: str) -> tuple[str, ...]:
        try:
            argv = self.applications[application_id]
        except KeyError as exc:
            raise SpecError(
                f"La aplicación '{application_id}' no está registrada en este equipo."
            ) from exc
        if not argv:
            raise SpecError(f"La aplicación '{application_id}' no tiene un lanzador válido.")
        return tuple(str(item) for item in argv)

    def resolve_command(self, command_id: str) -> tuple[str, ...]:
        try:
            argv = self.commands[command_id]
        except KeyError as exc:
            raise SpecError(
                f"La comprobación '{command_id}' no está registrada en este Styler."
            ) from exc
        if not argv:
            raise SpecError(f"La comprobación '{command_id}' no tiene una orden válida.")
        return tuple(str(item) for item in argv)

    def resolve_path(self, raw: str) -> Path:
        """Expande una ruta declarada, confinada al HOME.

        Se apoya en el mismo guardián que ya usa el catálogo de componentes:
        nada de rutas absolutas fuera del HOME ni escapes con ``..``.
        """
        from styler.component_catalog.paths import expand_user_path

        return expand_user_path(raw, home=self.home)


ConditionFactory = Callable[[ConditionSpec, Sequence[Condition], BuildContext], Condition]
ActionFactory = Callable[[ActionSpec, Sequence[Action], BuildContext], Action]


class ConditionRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ConditionFactory] = {}

    def register(self, kind: str, factory: ConditionFactory) -> None:
        self._factories[kind] = factory

    def known_kinds(self) -> set[str]:
        return set(self._factories)

    def validate(self, spec: ConditionSpec) -> None:
        if spec.kind not in self._factories:
            raise UnknownKindError(f"Condición desconocida: '{spec.kind}'.")
        for child in spec.children:
            self.validate(child)

    def build(self, spec: ConditionSpec, context: BuildContext | None = None) -> Condition:
        ctx = context or BuildContext()
        self.validate(spec)
        children = [self.build(child, ctx) for child in spec.children]
        return self._factories[spec.kind](spec, children, ctx)


class ActionRegistry:
    def __init__(self, conditions: ConditionRegistry | None = None) -> None:
        self._factories: dict[str, ActionFactory] = {}
        self.conditions = conditions or default_condition_registry()

    def register(self, kind: str, factory: ActionFactory) -> None:
        self._factories[kind] = factory

    def known_kinds(self) -> set[str]:
        return set(self._factories)

    def validate(self, spec: ActionSpec) -> None:
        if spec.kind not in self._factories:
            raise UnknownKindError(
                f"Acción desconocida: '{spec.kind}'. "
                "Un paquete no puede introducir acciones nuevas: solo puede usar el catálogo."
            )
        if spec.condition is not None:
            self.conditions.validate(spec.condition)
        for abort_condition in spec.abort_conditions:
            self.conditions.validate(abort_condition)
        for child in spec.children:
            self.validate(child)

    def build(self, spec: ActionSpec, context: BuildContext | None = None) -> Action:
        ctx = context or BuildContext()
        self.validate(spec)
        children = [self.build(child, ctx) for child in spec.children]
        return self._factories[spec.kind](spec, children, ctx)

    def build_condition(self, spec: ConditionSpec, context: BuildContext | None = None) -> Condition:
        return self.conditions.build(spec, context)


# ---------------------------------------------------------------------------
# Fábricas del catálogo estándar
# ---------------------------------------------------------------------------


def _need(spec: Any, key: str) -> Any:
    if key not in spec.params:
        raise SpecError(f"'{spec.kind}' requiere el parámetro '{key}'.")
    return spec.params[key]


def _one_child(children: Sequence[Action], kind: str) -> Action:
    if len(children) != 1:
        raise SpecError(f"'{kind}' requiere exactamente una acción hija.")
    return children[0]


def default_condition_registry() -> ConditionRegistry:
    registry = ConditionRegistry()

    def path_exists(spec, children, ctx):
        return FileExistsCondition(ctx.resolve_path(str(_need(spec, "path"))))

    def path_glob(spec, children, ctx):
        return PathGlobCondition(
            ctx.resolve_path(str(_need(spec, "base"))),
            str(_need(spec, "pattern")),
            require_dir=bool(spec.params.get("require_dir", False)),
        )

    def process_running(spec, children, ctx):
        return ProcessRunningCondition(str(_need(spec, "process_name")))

    def process_alive(spec, children, ctx):
        variable = str(spec.params.get("variable", "last_process"))

        def pid() -> int | None:
            process = ctx.variables.get(variable)
            return getattr(process, "pid", None) if process is not None else None

        return ProcessAliveCondition(pid)

    def file_stable(spec, children, ctx):
        return FileStableCondition(
            ctx.resolve_path(str(_need(spec, "path"))),
            stable_for_seconds=float(spec.params.get("stable_for_seconds", 2.0)),
        )

    def directory_quiescent(spec, children, ctx):
        return DirectoryQuiescentCondition(
            ctx.resolve_path(str(_need(spec, "path"))),
            stable_for_seconds=float(spec.params.get("stable_for_seconds", 2.0)),
        )

    def dbus_name(spec, children, ctx):
        return DBusNameOwnedCondition(str(_need(spec, "bus_name")))

    def command_output(spec, children, ctx):
        argv = _need(spec, "argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
            raise SpecError("'command_output' requiere 'argv' como lista no vacía.")
        return CommandOutputCondition(
            [str(item) for item in argv],
            str(spec.params.get("pattern", "")),
            expect_returncode=spec.params.get("expect_returncode", 0),
        )

    def registered_command_output(spec, children, ctx):
        command_id = str(_need(spec, "command_id"))
        return CommandOutputCondition(
            list(ctx.resolve_command(command_id)),
            str(spec.params.get("pattern", "")),
            expect_returncode=spec.params.get("expect_returncode", 0),
        )

    def window_present(spec, children, ctx):
        return WindowPresentCondition(str(_need(spec, "pattern")))

    def cpu_below(spec, children, ctx):
        variable = str(spec.params.get("variable", "last_process"))

        def pid() -> int | None:
            process = ctx.variables.get(variable)
            return getattr(process, "pid", None) if process is not None else None

        return CpuBelowCondition(
            pid,
            threshold_percent=float(spec.params.get("threshold_percent", 5.0)),
            stable_for_seconds=float(spec.params.get("stable_for_seconds", 2.0)),
        )

    def desktop_element(expectation):
        def factory(spec, children, ctx):
            locator = ElementLocator.from_mapping(_need(spec, "locator"))
            return DesktopElementCondition(
                ctx.desktop_driver, locator, expectation=expectation
            )
        return factory

    def all_of(spec, children, ctx):
        return AllCondition(spec.name or "todas las condiciones", list(children))

    def any_of(spec, children, ctx):
        return AnyCondition(spec.name or "alguna condición", list(children))

    def gone(spec, children, ctx):
        if len(children) != 1:
            raise SpecError("'gone' requiere exactamente una condición hija.")
        return GoneCondition(children[0], name=spec.name)

    registry.register("path_exists", path_exists)
    registry.register("path_glob", path_glob)
    registry.register("process_running", process_running)
    registry.register("process_alive", process_alive)
    registry.register("file_stable", file_stable)
    registry.register("directory_quiescent", directory_quiescent)
    registry.register("dbus_name", dbus_name)
    registry.register("command_output", command_output)
    registry.register("registered_command_output", registered_command_output)
    registry.register("window_present", window_present)
    registry.register("cpu_below", cpu_below)
    registry.register("element_present", desktop_element("present"))
    registry.register("element_visible", desktop_element("visible"))
    registry.register("element_enabled", desktop_element("enabled"))
    registry.register("element_clickable", desktop_element("clickable"))
    registry.register("all", all_of)
    registry.register("any", any_of)
    registry.register("gone", gone)
    return registry


def default_action_registry(conditions: ConditionRegistry | None = None) -> ActionRegistry:
    registry = ActionRegistry(conditions)

    def note(spec, children, ctx):
        return NoteAction(str(spec.params.get("message", spec.title or spec.id or "nota")))

    def sleep(spec, children, ctx):
        return SleepAction(float(_need(spec, "seconds")))

    def wait_until_action(spec, children, ctx):
        if spec.condition is None:
            raise SpecError("'wait_until' requiere una condición.")
        return WaitAction(
            registry.build_condition(spec.condition, ctx),
            timeout_seconds=float(spec.params.get("timeout_seconds", 20.0)),
            poll_interval_seconds=float(spec.params.get("poll_interval_seconds", 0.5)),
            success_stable_seconds=float(spec.params.get("success_stable_seconds", 0.0)),
            abort_conditions=[registry.build_condition(item, ctx) for item in spec.abort_conditions],
        )

    def launch(spec, children, ctx):
        argv = _need(spec, "argv")
        if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
            raise SpecError("'launch_process' requiere 'argv' como lista no vacía.")
        return LaunchProcessAction([str(item) for item in argv])

    def launch_application(spec, children, ctx):
        application_id = str(_need(spec, "application_id"))
        return LaunchProcessAction(list(ctx.resolve_application(application_id)))

    def stop(spec, children, ctx):
        return StopProcessAction(
            variable=str(spec.params.get("variable", "last_process")),
            grace_seconds=float(spec.params.get("grace_seconds", 5.0)),
        )

    def desktop_click(spec, children, ctx):
        locator = ElementLocator.from_mapping(_need(spec, "locator"))
        return DesktopClickAction(ctx.desktop_driver, locator)

    def sequence(spec, children, ctx):
        if not children:
            raise SpecError("'sequence' requiere al menos una acción hija.")
        return SequenceAction(spec.title or spec.id or "secuencia", list(children))

    def retry(spec, children, ctx):
        return RetryAction(
            _one_child(children, spec.kind),
            attempts=int(spec.params.get("attempts", 2)),
            delay_seconds=float(spec.params.get("delay_seconds", 0.0)),
        )

    def timeout(spec, children, ctx):
        return TimeoutAction(
            _one_child(children, spec.kind), seconds=float(_need(spec, "seconds"))
        )

    def if_action(spec, children, ctx):
        if spec.condition is None:
            raise SpecError("'if' requiere una condición.")
        if not children:
            raise SpecError("'if' requiere al menos la rama verdadera.")
        return ConditionalAction(
            registry.build_condition(spec.condition, ctx),
            children[0],
            children[1] if len(children) > 1 else None,
        )

    def try_finally(spec, children, ctx):
        if len(children) != 2:
            raise SpecError("'try_finally' requiere dos hijas: cuerpo y limpieza.")
        return TryFinallyAction(spec.title or spec.id or "bloque protegido", children[0], children[1])

    def for_each(spec, children, ctx):
        values = _need(spec, "values")
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise SpecError("'for_each' requiere 'values' como lista.")
        return ForEachAction(
            str(_need(spec, "variable")), list(values), _one_child(children, spec.kind)
        )

    def with_application(spec, children, ctx):
        profile_id = str(_need(spec, "profile"))
        profile = ctx.profiles.get(profile_id)
        if profile is None:
            raise SpecError(
                f"No hay perfil de aplicación registrado con el id '{profile_id}'."
            )
        application_id = str(spec.params.get("application_id") or "")
        if application_id:
            argv = list(ctx.resolve_application(application_id))
        else:
            argv = _need(spec, "argv")
            if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
                raise SpecError(
                    "'with_application' requiere 'application_id' registrado o argv local confiable."
                )
            argv = [str(item) for item in argv]
        if not children:
            raise SpecError("'with_application' requiere un cuerpo.")
        body = (
            children[0]
            if len(children) == 1
            else SequenceAction(spec.title or "cuerpo", list(children))
        )
        return WithApplicationAction(
            spec.title or profile_id,
            LaunchProcessAction(argv),
            profile,
            body,
        )

    registry.register("note", note)
    registry.register("sleep", sleep)
    registry.register("wait_until", wait_until_action)
    registry.register("launch_process", launch)
    registry.register("launch_application", launch_application)
    registry.register("desktop_click", desktop_click)
    registry.register("stop_process", stop)
    registry.register("sequence", sequence)
    registry.register("retry", retry)
    registry.register("timeout", timeout)
    registry.register("if", if_action)
    registry.register("try_finally", try_finally)
    registry.register("for_each", for_each)
    registry.register("with_application", with_application)
    return registry


def dumps(spec: ActionSpec) -> str:
    return json.dumps(spec.to_dict(), indent=2, ensure_ascii=False)


def loads(raw: str) -> ActionSpec:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecError(f"El árbol de acciones no es JSON válido: {exc}") from exc
    return ActionSpec.from_dict(data)
